"""Import dialog: jsonreshape/gui/importdialog.py (docs/ARCHITECTURE.md §17) and its File menu entry.

The detected type and the option boxes per type, the default target (``.jsonl``; ``.json`` for YAML), a CSV
converted offscreen (the report headline, the notes, the task), "open the result" (the callback and the default
``app.request_open``; a JSON Lines result opens with ``format=jsonl``), the target never being the source, the
overwrite question, a target shown in another tab, cancel (nothing written), a malformed input (the inline error, no
error dialog), the options mapping of the widgets, the main window's action and ``show_import``, and the English texts.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from jsonreshape import codec
from jsonreshape.codec import JsonFloat
from jsonreshape.errors import Cancelled, UserInputError
from jsonreshape.gui import importdialog as idlg
from jsonreshape.gui import widgets
from jsonreshape.gui.context import AppContext
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.importdialog import ImportDialog, default_target, show_import_dialog
from jsonreshape.i18n import t

pytestmark = pytest.mark.gui

TIMEOUT = 30000
CSV = "id,name,price,contact.city,active,note\n1,Olma,1.10,Toshkent,true,\n007,Anor,2,Samarqand,false,x\n"
RECORDS = [
    {"id": 1, "name": "Olma", "price": JsonFloat("1.10"), "contact": {"city": "Toshkent"}, "active": True, "note": None},
    {"id": "007", "name": "Anor", "price": 2, "contact": {"city": "Samarqand"}, "active": False, "note": "x"},
]


@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(10)
    ctx.errors = []
    ctx.statuses = []
    ctx.open_calls = []
    ctx.error_reported.connect(ctx.errors.append)
    ctx.status_message.connect(lambda text, _ms: ctx.statuses.append(text))
    ctx.open_requested.connect(lambda path, opts: ctx.open_calls.append((path, opts)))
    yield ctx
    for w in list(widgets._kept_alive):
        w.close()
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def yes(monkeypatch):
    monkeypatch.setattr(widgets, "confirm_handler", lambda *_a: True)


@pytest.fixture
def no(monkeypatch):
    monkeypatch.setattr(widgets, "confirm_handler", lambda *_a: False)


@pytest.fixture
def dialogs(qtbot, app, yes):
    """Dialogs whose questions (overwrite the target) are answered "yes" unless a test installs its own handler."""
    made: list[ImportDialog] = []

    def make(source: Any = None, **kw: Any) -> ImportDialog:
        d = ImportDialog(app, source, **kw)
        qtbot.addWidget(d)
        d.show()
        made.append(d)
        return d

    yield make
    for d in made:
        if idlg._alive(d) and d.isVisible():
            d._job = None
            d.done(0)


def write(path: Path, data: str | bytes) -> Path:
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return path


def lines(path: Path) -> list[Any]:
    return [codec.loads(line) for line in path.read_bytes().splitlines()]


def finish(qtbot, d: ImportDialog) -> Any:
    """Run the import and wait until it ended (finished, failed or cancelled); returns the report (or None)."""
    ended: list = []
    signals = (d.run_finished, d.run_failed, d.run_cancelled)
    for sig in signals:
        sig.connect(lambda *_a: ended.append(True))
    try:
        assert d.run_import() is not None, d.error_text()
        qtbot.waitUntil(lambda: bool(ended), timeout=TIMEOUT)
    finally:
        for sig in signals:
            sig.disconnect()
    return d.report


# ----------------------------------------------------------------------------------------
# types, targets, options
# ----------------------------------------------------------------------------------------
def test_detected_type_and_default_target(qtbot, app, dialogs, tmp_path) -> None:
    src = write(tmp_path / "data.csv", CSV)
    d = dialogs(src)
    assert d.windowTitle() == "Import — data.csv"
    assert d.detected_label.text() == "Aniqlangan tur: CSV / TSV" and d.current_format() == "csv"
    assert d.target_path() == str(tmp_path / "data.jsonl") and d.output_format() == "jsonl"
    assert d.csv_box.isVisible() and not d.xml_box.isVisible() and not d.indent_combo.isEnabled()
    d.set_source_path(tmp_path / "a.xml")
    assert d.current_format() == "xml" and d.xml_box.isVisible() and not d.csv_box.isVisible()
    assert d.target_path() == str(tmp_path / "a.jsonl")
    d.set_source_path(tmp_path / "a.yml")
    assert d.target_path() == str(tmp_path / "a.json") and d.indent_combo.isEnabled()
    d.set_source_path(tmp_path / "a.txt")
    assert d.detected_label.text() == t("gui.importdialog.detected.none") and d.current_format() is None
    assert d.csv_box.isVisible()                       # unknown: the CSV options stay reachable
    d.format_combo.setCurrentIndex(d.format_combo.findData("xml"))
    assert d.current_format() == "xml" and d.xml_box.isVisible() and d.target_path() == str(tmp_path / "a.jsonl")
    d.format_combo.setCurrentIndex(d.format_combo.findData("yaml"))
    assert d.target_path() == str(tmp_path / "a.json")
    # a target chosen by hand stays
    d.set_target_path(tmp_path / "mine.jsonl")
    d.set_source_path(tmp_path / "other.csv")
    assert d.target_path() == str(tmp_path / "mine.jsonl")
    assert default_target("x/a.csv") == os.path.join("x", "a.jsonl") and default_target("a.yaml") == "a.json"
    assert default_target("a.txt", "yaml") == "a.json" and default_target("noext") == "noext.jsonl"
    d2 = dialogs()
    assert d2.windowTitle() == "Import (CSV, YAML, XML)" and not d2.run_button.isEnabled()


def test_options_follow_the_widgets(qtbot, app, dialogs, tmp_path) -> None:
    d = dialogs(write(tmp_path / "data.csv", CSV))
    opts = d.options()
    assert opts.fmt is None and opts.output == "jsonl" and opts.indent == 2 and opts.types == "auto"
    assert opts.empty == "null" and opts.delimiter is None and opts.header and opts.nest and opts.encoding is None
    assert not opts.skip_bad and opts.record_tag is None and opts.record_path is None
    d.delimiter_combo.setCurrentIndex(d.delimiter_combo.findData("\t"))
    d.encoding_combo.setCurrentIndex(d.encoding_combo.findData("cp1251"))
    d.types_combo.setCurrentIndex(d.types_combo.findData("string"))
    d.empty_combo.setCurrentIndex(d.empty_combo.findData("string"))
    d.header_check.setChecked(False)
    d.nest_check.setChecked(False)
    d.skip_bad_check.setChecked(True)
    d.indent_combo.setCurrentIndex(d.indent_combo.findData("\t"))
    d.set_target_path(tmp_path / "o.json")
    d.record_tag_edit.setText(" item ")
    opts = d.options()
    assert opts.delimiter == "\t" and opts.encoding == "cp1251" and opts.types == "string" and opts.empty == "string"
    assert not opts.header and not opts.nest and opts.skip_bad and opts.indent == "\t" and opts.output == "json"
    assert opts.record_tag == "item"
    d.encoding_combo.setEditText("koi8-r")
    assert d.options().encoding == "koi8-r"
    d.encoding_combo.setEditText("no-such-codec")
    with pytest.raises(UserInputError):
        d.options()
    assert d.run_import() is None and "Noma'lum kodirovka" in d.error_text()


# ----------------------------------------------------------------------------------------
# running
# ----------------------------------------------------------------------------------------
def test_import_converts_and_opens_the_result(qtbot, app, dialogs, tmp_path) -> None:
    src = write(tmp_path / "data.csv", CSV)
    opened: list = []
    d = dialogs(src, open_result=lambda path, opts: opened.append((path, opts)))
    rep = finish(qtbot, d)
    target = tmp_path / "data.jsonl"
    assert rep is not None and rep.records == 2 and target.exists() and lines(target) == RECORDS
    assert '"price":1.10' in target.read_text(encoding="utf-8") and src.read_text(encoding="utf-8") == CSV
    assert d.headline_text().startswith("2 ta yozuv: data.csv (CSV / TSV) → data.jsonl (JSON Lines, ")
    assert d.notes_label.text().splitlines() == ["3 ta katak son bo'ldi.", "2 ta katak true / false / null bo'ldi.",
                                                 "1 ta bo'sh katak null bo'ldi."]
    assert not d.warnings_label.isVisible() and d.report_box.isVisible() and d.open_button.isEnabled()
    assert d.error_text() == "" and d.status_text() == "" and not d.running and app.errors == []
    assert app.statuses[-1] == f"Import qilindi: {target}"
    assert d.open_result() and opened == [(str(target), {"format": "jsonl"})]


def test_open_result_defaults_to_a_new_tab(qtbot, app, dialogs, tmp_path) -> None:
    src = write(tmp_path / "data.csv", CSV)
    d = dialogs(src)
    d.set_target_path(tmp_path / "data.json")
    d.indent_combo.setCurrentIndex(d.indent_combo.findData(4))
    rep = finish(qtbot, d)
    assert rep.output_fmt == "json" and (tmp_path / "data.json").read_bytes() == codec.dumps(RECORDS, indent=4)
    assert d.open_result() and app.open_calls == [(str(tmp_path / "data.json"), {})]
    d.set_source_path(write(tmp_path / "s.yaml", "---\na: 1\n---\nb: 2\n"))
    assert d.target_path() == str(tmp_path / "data.json")        # chosen by hand: kept
    d.set_target_path(tmp_path / "s.jsonl")
    rep = finish(qtbot, d)
    assert rep.records == 2 and lines(tmp_path / "s.jsonl") == [{"a": 1}, {"b": 2}]
    assert d.open_result() and app.open_calls[-1] == (str(tmp_path / "s.jsonl"), {"format": "jsonl"})
    assert "(YAML)" in d.headline_text()


def test_xml_with_record_tag_and_warnings(qtbot, app, dialogs, tmp_path) -> None:
    src = write(tmp_path / "c.xml", '<c><g><item id="1"/></g><g><item id="2"/></g></c>')
    d = dialogs(src)
    d.record_tag_edit.setText("item")
    rep = finish(qtbot, d)
    assert rep.records == 2 and lines(tmp_path / "c.jsonl") == [{"@id": 1}, {"@id": 2}]
    d.record_tag_edit.setText("")
    d.record_path_edit.setText("nope/x")
    rep = finish(qtbot, d)
    assert rep.records == 0 and d.warnings_label.isVisible()
    assert d.warnings_label.text().startswith("Birorta ham yozuv topilmadi (ildiz elementi: <c>).")


def test_target_is_never_the_source(qtbot, app, dialogs, tmp_path) -> None:
    src = write(tmp_path / "data.csv", CSV)
    d = dialogs(src)
    d.set_target_path(src)
    assert d.run_import() is None
    assert d.error_text().startswith(t("writer.error.same_file", path=str(src)))
    assert t("gui.importdialog.same_file.hint") in d.error_text() and not d.running
    if os.name == "nt":
        d.set_target_path(tmp_path / "DATA.CSV")
        assert d.run_import() is None and "DATA.CSV" in d.error_text()
    d.set_target_path("")
    assert d.run_import() is None and d.error_text() == t("gui.importdialog.target.need")
    d2 = dialogs()
    d2.set_target_path(tmp_path / "x.jsonl")
    assert d2.run_import() is None and d2.error_text() == t("gui.importdialog.source.need")
    d2.set_source_path(tmp_path / "missing.csv")
    assert d2.run_import() is None and "topilmadi" in d2.error_text()
    d2.set_source_path(write(tmp_path / "data.txt", CSV))
    assert d2.run_import() is None and t("gui.importdialog.unknown_format.hint") in d2.error_text()
    assert src.read_text(encoding="utf-8") == CSV


def test_overwrite_is_asked(qtbot, app, dialogs, tmp_path, monkeypatch) -> None:
    src = write(tmp_path / "data.csv", CSV)
    target = write(tmp_path / "data.jsonl", "old\n")
    d = dialogs(src)
    questions: list[str] = []
    monkeypatch.setattr(widgets, "confirm_handler", lambda _p, text, _t: questions.append(text) or False)
    assert d.run_import() is None and target.read_text() == "old\n" and not d.running
    assert questions == [t("gui.importdialog.overwrite", path=str(target))]
    monkeypatch.setattr(widgets, "confirm_handler", lambda *_a: True)
    finish(qtbot, d)
    assert lines(target) == RECORDS


def test_malformed_input_shows_the_error_inline(qtbot, app, dialogs, tmp_path) -> None:
    src = write(tmp_path / "w.csv", "a,b\n1,2\n3\n")
    d = dialogs(src)
    failed: list = []
    d.run_failed.connect(failed.append)
    finish(qtbot, d)
    assert len(failed) == 1 and failed[0].key == "importers.error.csv_row_width"
    assert d.error_text().startswith("Bajarilmadi: 3-qator: 1 ta maydon, sarlavhada esa 2 ta.")
    assert "--skip-bad" in d.error_text() and not (tmp_path / "w.jsonl").exists()
    assert app.errors == [] and d.report is None and not d.report_box.isVisible() and not d.open_button.isEnabled()
    d.skip_bad_check.setChecked(True)
    rep = finish(qtbot, d)
    assert rep.records == 1 and d.error_text() == "" and d.warnings_label.isVisible()
    assert d.warnings_label.text() == "1 ta buzuq qator o'tkazib yuborildi (3-qator)."


def test_cancel_writes_nothing(qtbot, app, dialogs, tmp_path, monkeypatch) -> None:
    src = write(tmp_path / "data.csv", CSV)

    def slow_import(_src: Any, _tgt: Any, _opts: Any, *, settings: Any, progress: Any, cancel: Any) -> Any:
        progress.set_total(1000)
        while not cancel.cancelled:
            progress.update(10)
            time.sleep(0.005)
        raise Cancelled()

    monkeypatch.setattr(idlg, "import_file", slow_import)
    d = dialogs(src)
    cancelled: list = []
    d.run_cancelled.connect(lambda: cancelled.append(True))
    task = d.run_import()
    assert task is not None and d.running and d.stop_button.isVisible() and not d.run_button.isEnabled()
    assert not d.options_box.isEnabled() and not d.source_edit.isEnabled()
    qtbot.waitUntil(lambda: d.status_text().startswith("CSV") or "/" in d.status_text() or True, timeout=TIMEOUT)
    d.cancel_run()
    qtbot.waitUntil(lambda: bool(cancelled), timeout=TIMEOUT)
    assert not d.running and d.status_text() == t("gui.importdialog.cancelled") and d.report is None
    assert not (tmp_path / "data.jsonl").exists() and d.run_button.isEnabled() and d.options_box.isEnabled()
    assert app.errors == []


def test_close_while_running_asks_and_stops(qtbot, app, dialogs, tmp_path, monkeypatch) -> None:
    def slow_import(_src: Any, _tgt: Any, _opts: Any, *, settings: Any, progress: Any, cancel: Any) -> Any:
        while not cancel.cancelled:
            time.sleep(0.005)
        raise Cancelled()

    monkeypatch.setattr(idlg, "import_file", slow_import)
    d = dialogs(write(tmp_path / "data.csv", CSV))
    task = d.run_import()
    monkeypatch.setattr(widgets, "confirm_handler", lambda *_a: False)
    d.done(0)
    assert d.isVisible() and d.running                      # declined: it keeps running
    monkeypatch.setattr(widgets, "confirm_handler", lambda *_a: True)
    d.done(0)
    assert not d.isVisible()
    qtbot.waitUntil(lambda: task.state != "running", timeout=TIMEOUT)
    assert task.state == "cancelled"


def test_target_shown_in_a_tab_with_edits_is_refused(qtbot, app, dialogs, tmp_path, monkeypatch) -> None:
    from jsonreshape.gui import editing

    src = write(tmp_path / "data.csv", CSV)
    target = write(tmp_path / "data.jsonl", '{"a": 1}\n')
    monkeypatch.setattr(editing, "target_conflict", lambda path, exclude=None: ("unsaved", []))
    d = dialogs(src)
    assert d.run_import() is None and not d.running
    assert d.error_text() == t("gui.edit.target.unsaved", path=str(target)) + " " + t("gui.edit.target.unsaved.hint")
    assert target.read_text() == '{"a": 1}\n'


def test_show_import_dialog_reuses_the_open_window(qtbot, app, tmp_path) -> None:
    src = write(tmp_path / "data.csv", CSV)
    d1 = show_import_dialog(app, src)
    qtbot.addWidget(d1)
    d2 = show_import_dialog(app, tmp_path / "other.csv")
    assert d2 is d1 and d1.source_path() == str(tmp_path / "other.csv")
    d1._job = None
    d1.done(0)
    d3 = show_import_dialog(app)
    qtbot.addWidget(d3)
    assert d3 is not d1
    d3.done(0)


def test_english_texts(qtbot, app, dialogs, tmp_path, lang_en) -> None:
    src = write(tmp_path / "data.csv", CSV)
    d = dialogs(src)
    assert d.windowTitle() == "Import — data.csv" and d.detected_label.text() == "Detected type: CSV / TSV"
    assert d.run_button.text() == "&Import" and d.csv_box.title() == "CSV / TSV"
    rep = finish(qtbot, d)
    assert rep.records == 2 and d.headline_text().startswith("2 records: data.csv (CSV / TSV) → data.jsonl (JSON Lines, ")
    assert d.notes_label.text().splitlines()[0] == "3 cells became numbers."
    assert app.statuses[-1].startswith("Imported: ")


# ----------------------------------------------------------------------------------------
# the main window
# ----------------------------------------------------------------------------------------
def test_main_window_action_and_menu(qtbot, tmp_path, lang_uz) -> None:
    from guishell_helpers import make_app, stub_panels, stub_registry, wait_ready
    from jsonreshape.gui.mainwindow import MainWindow

    ctx = make_app(tmp_path)
    ctx.open_calls = []
    window = MainWindow(ctx, view_registry=stub_registry(), panel_factories=stub_panels())
    qtbot.addWidget(window)
    try:
        action = window.acts["import_file"]
        assert action.text() == "&Import (CSV, YAML, XML)…" and action.isEnabled()
        file_actions = [a.text() for a in window.menus["file"].actions() if a.text()]
        # the first group of the File menu: Open, Recent, Reopen, Record path, Import
        assert file_actions.index("&Import (CSV, YAML, XML)…") == file_actions.index("&Ochish…") + 4
        assert file_actions[file_actions.index("&Import (CSV, YAML, XML)…") - 1] == t("gui.main.act.record_path")
        src = write(tmp_path / "data.csv", CSV)
        dialog = window.show_import(src)              # a child of the window: deleted with it, not by qtbot
        assert dialog is window.dialogs["import"] and dialog.source_path() == str(src) and dialog.isVisible()
        assert window.show_import() is dialog
        rep = finish(qtbot, dialog)
        assert rep.records == 2
        tabs_before = window.tabs.count()
        assert dialog.open_result()
        qtbot.waitUntil(lambda: window.tabs.count() == tabs_before + 1, timeout=TIMEOUT)
        tab = window.current_tab()
        wait_ready(qtbot, tab.session)
        assert tab.session.state == "ready" and os.path.normcase(tab.session.path) == os.path.normcase(str(tmp_path / "data.jsonl"))
        assert tab.session.doc.record_count == 2
        dialog._job = None
        dialog.done(0)
        assert not idlg._alive(dialog) or not dialog.isVisible()
    finally:
        window.close()
        ctx.tasks.cancel_all()
        assert ctx.tasks.shutdown(30)
