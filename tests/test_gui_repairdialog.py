"""Repair dialog: jsonreshape/gui/repairdialog.py (docs/ARCHITECTURE.md §14.5, §14.9; s5/map_repair_diff.md §3.8).

Check (nothing written) and repair (the output equals ``repair_file``'s, both core backends), the summary by fix
code with the first location (``show_location`` callback), the fix grid, the truncation and unrepairable boxes,
"save the partial result", every fix saved as CSV / JSON / JSON Lines (also beyond the 10 000 listed), "open the
repaired file", cancel and progress, the target never being the source (other spellings, hard links), the report
always closed (new run, dialog closed, closed while running, an unrepairable report), inline errors, Uzbek and
English texts.
"""

from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from jsonreshape import repair
from jsonreshape.errors import Cancelled
from jsonreshape.gui import repairdialog as rd
from jsonreshape.gui import widgets
from jsonreshape.gui.context import AppContext
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.repairdialog import RepairDialog, RepairLocation, default_target, location_of
from jsonreshape.i18n import t
from jsonreshape.repair import RepairOptions, UnrepairableError, repair_file
from jsonreshape.tasks import format_bytes

pytestmark = pytest.mark.gui

TIMEOUT = 30000

BROKEN = ("[\n"
          '  {"a": NaN, "s": \'Oʻzbek "x"\'},\n'
          '  {"b": 2,},  // izoh\n'
          '  {"c": [1, 2,], "n": 1e400, "m": 12345678901234567890, "z": -0.0, "f": 1.10}\n'
          "]\n").encode("utf-8")
TRUNCATED = b'[{"a": 1}, {"b": 2}, {"c": [1, 2'
UNREPAIRABLE = b'[{"a": 1}, {"b": 2}, [1, 2}, {"d": 4}]'
VALID = '[{"a": 1}, {"b": "Самарқанд"}]\n'.encode("utf-8")


def many_nans(n: int) -> bytes:
    return b"[" + b",".join(b'{"v": NaN}' for _ in range(n)) + b"]"


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
def dialogs(qtbot, app):
    made: list[RepairDialog] = []

    def make(source: Any = None, **kw: Any) -> RepairDialog:
        d = RepairDialog(app, source, **kw)
        qtbot.addWidget(d)
        d.show()
        made.append(d)
        return d

    yield make
    for d in made:
        if rd._alive(d) and d.isVisible():
            d._job = None
            d.done(0)


@pytest.fixture
def backend(core_backend):
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = repair.set_scanner_backend(name)
    yield name
    repair.set_scanner_backend(old)


def write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def finish(qtbot, d: RepairDialog, start: Any) -> Any:
    """Run ``start()`` (check / repair / save_partial) and wait until the run ended (finished, failed or
    cancelled); returns the report shown."""
    ended: list = []

    def mark(*_args: Any) -> None:
        ended.append(True)

    signals = (d.run_finished, d.run_failed, d.run_cancelled)
    for sig in signals:
        sig.connect(mark)
    try:
        assert start() is not None, d.error_text()
        qtbot.waitUntil(lambda: bool(ended), timeout=TIMEOUT)
    finally:
        for sig in signals:
            sig.disconnect(mark)
    return d.report


def cell(grid: Any, row: int, column_id: str) -> Any:
    column = next(c for c in grid.columns() if c.id == column_id)
    return grid.source().cell(row, column)


def mnemonics(widget: Any) -> list[str]:
    """The Alt+letter mnemonics of the labels and buttons of ``widget`` (lower case)."""
    from PySide6.QtWidgets import QAbstractButton, QLabel

    out = []
    for cls in (QLabel, QAbstractButton):
        for w in widget.findChildren(cls):
            text = w.text().replace("&&", "")
            i = text.find("&")
            if i != -1 and i + 1 < len(text):
                out.append(text[i + 1].lower())
    return out


# ----------------------------------------------------------------------------------------
# check / repair
# ----------------------------------------------------------------------------------------
def test_check_reports_and_writes_nothing(qtbot, app, dialogs, tmp_path, backend) -> None:
    src = write(tmp_path / "buzuq.json", BROKEN)
    d = dialogs(src)
    target = Path(d.target_path())
    assert target == tmp_path / "buzuq.repaired.json"
    rep = finish(qtbot, d, d.check)
    assert rep.check_only and not rep.written and not target.exists()
    assert dict(rep.counts) == {"nan": 1, "single_quote": 1, "trailing_comma": 2, "comment": 1}
    assert d.headline_text().startswith("Tekshirildi: buzuq.json")
    # the summary: one row per code, in the order of the first location in the file
    rows = d.summary_rows_of(rep)
    assert [r[0] for r in rows] == ["nan", "single_quote", "trailing_comma", "comment"]
    assert d.summary_row_count() == 4
    assert cell(d.summary_grid, 0, "fix").text == "NaN → null"
    assert cell(d.summary_grid, 0, "count").text == "1"
    assert cell(d.summary_grid, 2, "count").text == "2"
    first = rep.first["nan"]
    assert cell(d.summary_grid, 0, "first").text == f"{first.line}-qator, {first.column}-ustun"
    assert cell(d.summary_grid, 0, "category").text == t("repair.category.data")
    assert "NaN → null 1 ta" in d.data_label.text() and d.data_label.isVisible()
    assert d.kind_label.text() == "Format: JSON massiv → natija: JSON massiv"
    assert not d.truncation_box.isVisible() and not d.problem_box.isVisible()
    assert d.fix_count_shown() == 5 and d.fixes_label.text() == "Barcha tuzatishlar (5 ta):"
    assert not d.open_button.isEnabled() and d.save_report_button.isEnabled()


def test_repair_writes_what_the_core_writes(qtbot, app, dialogs, tmp_path, backend) -> None:
    src = write(tmp_path / "buzuq.json", BROKEN)
    opened: list = []
    d = dialogs(src, open_repaired=lambda path, opts: opened.append((path, opts)))
    rep = finish(qtbot, d, d.repair)
    target = tmp_path / "buzuq.repaired.json"
    assert rep.written and target.exists() and not rep.check_only
    want = tmp_path / "want.json"
    with repair_file(src, want) as core_rep:
        assert dict(core_rep.counts) == dict(rep.counts)
    assert target.read_bytes() == want.read_bytes()
    text = target.read_text(encoding="utf-8")
    assert "1e400" in text and "12345678901234567890" in text and "-0.0" in text and "1.10" in text
    assert 'Oʻzbek \\"x\\"' in text and src.read_bytes() == BROKEN          # the source is never changed
    assert d.headline_text().startswith("Ta'mirlandi: buzuq.json → buzuq.repaired.json")
    assert d.open_button.isEnabled()
    assert d.open_repaired() and opened == [(str(target), {"format": "array"})]
    assert app.statuses[-1] == f"Ta'mirlandi: {target}"


def test_open_repaired_defaults_to_a_new_tab(qtbot, app, dialogs, tmp_path) -> None:
    src = write(tmp_path / "x.jsonl", b'{"a": 1}\n{"b": NaN}\n')
    d = dialogs(src)
    assert d.target_path().endswith("x.repaired.jsonl")
    finish(qtbot, d, d.repair)
    assert d.open_repaired()
    assert app.open_calls == [(str(tmp_path / "x.repaired.jsonl"), {"format": "jsonl"})]


def test_locations_go_to_the_callback(qtbot, app, dialogs, tmp_path) -> None:
    src = write(tmp_path / "buzuq.json", BROKEN)
    places: list[RepairLocation] = []
    d = dialogs(src, show_location=places.append)
    rep = finish(qtbot, d, d.check)
    first = rep.first["nan"]
    loc = d.activate_summary(0)
    assert loc == RepairLocation(str(src), first.offset, first.line, first.column, max(1, first.length), "nan")
    assert places == [loc]
    # Enter on a summary row
    d.summary_grid.set_current(3, 0)
    d.summary_grid.setFocus()
    qtbot.keyClick(d.summary_grid, "\r")
    assert places[-1].what == "comment" and places[-1].line == 3
    # the fix grid lists every fix; activating one shows its place
    assert [cell(d.fixes_grid, r, "fix").text for r in range(d.fix_count_shown())] == [f.name for f in rep.fixes]
    assert cell(d.fixes_grid, 0, "line").text == str(rep.fixes[0].line)
    assert cell(d.fixes_grid, 0, "before").text == "NaN" and cell(d.fixes_grid, 0, "after").text == "null"
    removed = next(r for r, f in enumerate(rep.fixes) if f.code == "comment")
    assert cell(d.fixes_grid, removed, "after").text == "(hech narsa)"
    assert cell(d.fixes_grid, 0, "record").text == "#0"
    loc = d.activate_fix(removed)
    assert loc == location_of(src, rep.fixes[removed]) and places[-1] == loc
    assert d.copy_fixes([0]).split("\t")[:3] == ["NaN → null", str(rep.fixes[0].line), str(rep.fixes[0].column)]
    # without a callback the dialog says why nothing happens
    d2 = dialogs(src)
    finish(qtbot, d2, d2.check)
    assert d2.activate_summary(0) is None and d2.note_text() == t("gui.repairdialog.show_location.none")
    assert not d2.problem_button.isEnabled()


def test_truncation_box(qtbot, app, dialogs, tmp_path) -> None:
    src = write(tmp_path / "kesilgan.json", TRUNCATED)
    places: list = []
    d = dialogs(src, show_location=places.append)
    rep = finish(qtbot, d, d.repair)
    assert rep.written and rep.truncated is not None and rep.problem is None
    assert (tmp_path / "kesilgan.repaired.json").read_bytes() == b'[{"a": 1}, {"b": 2}]'
    assert d.truncation_box.isVisible() and not d.problem_box.isVisible()
    text = d.truncation_label.text()
    assert "1-qator, 20-ustun" in text and "2 ta yozuv" in text and "13 B" in text and "]" in text
    loc = d.show_truncation()
    assert loc is not None and loc.what == "truncated" and (loc.line, loc.column) == (1, 20) and places == [loc]


def test_unrepairable_then_save_the_partial_result(qtbot, app, dialogs, tmp_path) -> None:
    src = write(tmp_path / "yomon.json", UNREPAIRABLE)
    places: list = []
    failures: list = []
    d = dialogs(src, show_location=places.append)
    d.run_failed.connect(failures.append)
    rep = finish(qtbot, d, d.repair)
    target = tmp_path / "yomon.repaired.json"
    assert isinstance(failures[0], UnrepairableError) and failures[0].report is rep
    assert rep.problem.code == "mismatched_closer" and not rep.written and not target.exists()
    assert d.problem_box.isVisible() and not d.truncation_box.isVisible()
    assert "yopuvchi qavs" in d.problem_label.text()
    assert d.context_label.text() == rep.problem.context() and "^" in d.context_label.text()
    assert "Hech narsa yozilmadi." in d.partial_label.text() and "2 ta yozuv" in d.partial_label.text()
    assert d.save_partial_button.isVisible() and not app.errors
    loc = d.show_problem()
    assert loc.what == "mismatched_closer" and (loc.line, loc.column) == (1, 27)
    # "Qisman natijani saqlash": the same repair with allow_partial
    rep2 = finish(qtbot, d, d.save_partial)
    assert rep2 is not rep and rep2.written and rep2.problem is not None
    assert target.read_bytes() == b'[{"a": 1}, {"b": 2}]'
    assert "Qisman natija yozildi" in d.partial_label.text() and not d.save_partial_button.isVisible()
    assert d.save_partial() is None                      # written already


def test_check_with_a_problem_offers_the_partial_result(qtbot, app, dialogs, tmp_path) -> None:
    src = write(tmp_path / "yomon.json", UNREPAIRABLE)
    d = dialogs(src)
    rep = finish(qtbot, d, d.check)
    assert rep.check_only and rep.problem is not None and d.save_partial_button.isVisible()
    rep2 = finish(qtbot, d, d.save_partial)
    assert rep2.written and (tmp_path / "yomon.repaired.json").read_bytes() == b'[{"a": 1}, {"b": 2}]'


def test_many_fixes_listed_and_every_fix_saved(qtbot, app, dialogs, tmp_path, yes) -> None:
    src = write(tmp_path / "nan.json", many_nans(10_050))
    saved: list = []
    d = dialogs(src)
    d.report_saved.connect(saved.append)
    rep = finish(qtbot, d, d.check)
    assert rep.total == 10_050 and len(rep.fixes) == 10_000 and rep.fixes_file is not None
    assert d.fix_count_shown() == 10_000
    assert d.fixes_label.text() == "Ro'yxatda dastlabki 10 000 ta tuzatish (jami 10 050 ta); hammasini " \
                                   "\"Hisobotni saqlash\" orqali olish mumkin."
    out_csv = tmp_path / "hisobot.csv"
    with qtbot.waitSignal(d.report_saved, timeout=TIMEOUT):
        assert d.save_report(out_csv) is not None
    rows = list(csv.reader(io.StringIO(out_csv.read_bytes().decode("utf-8-sig"))))
    assert rows[0][:3] == ["code", "name", "category"] and len(rows) == 10_051
    assert rows[-1][0] == "nan" and rows[-1][1] == "NaN → null"
    with qtbot.waitSignal(d.report_saved, timeout=TIMEOUT):
        d.save_report(tmp_path / "hisobot.jsonl")
    lines = (tmp_path / "hisobot.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 10_050 and json.loads(lines[0])["code"] == "nan"
    asked: list = []
    d._ask_save = lambda *a: (asked.append(a), str(tmp_path / "hisobot.json"))[1]
    with qtbot.waitSignal(d.report_saved, timeout=TIMEOUT):
        d.save_report()
    assert asked[0][2] == str(tmp_path / "nan.repair-report.csv")
    assert len(json.loads((tmp_path / "hisobot.json").read_text(encoding="utf-8"))) == 10_050
    assert saved == [str(out_csv), str(tmp_path / "hisobot.jsonl"), str(tmp_path / "hisobot.json")]
    # the report may not overwrite the source, nor have an unknown extension
    assert d.save_report(src) is None and d.error_text()
    assert d.save_report(tmp_path / "x.txt") is None and ".csv" in d.error_text()


def test_the_report_is_always_closed(qtbot, app, dialogs, tmp_path, yes) -> None:
    src = write(tmp_path / "nan.json", many_nans(10_020))
    d = dialogs(src)
    rep1 = finish(qtbot, d, d.check)
    f1 = rep1.fixes_file
    assert f1 is not None and f1.exists()
    rep2 = finish(qtbot, d, d.repair)                 # a new run closes the previous report
    assert not f1.exists() and rep1.fixes_file is None
    f2 = rep2.fixes_file
    assert f2 is not None and f2.exists()
    d.close()                                         # closing the dialog closes the report shown
    assert not f2.exists() and d.report is None
    # an unrepairable report too
    bad = write(tmp_path / "yomon.json", many_nans(10_020)[:-1] + b",[1}]")
    d2 = dialogs(bad)
    rep3 = finish(qtbot, d2, d2.repair)
    assert rep3.problem is not None and rep3.fixes_file is not None and rep3.fixes_file.exists()
    f3 = rep3.fixes_file
    d2.close()
    assert not f3.exists()


def test_a_report_being_saved_stays_open_until_the_save_ended(qtbot, app, dialogs, tmp_path) -> None:
    src = write(tmp_path / "nan.json", many_nans(10_020))
    d = dialogs(src)
    rep = finish(qtbot, d, d.check)
    fixes = rep.fixes_file
    assert fixes is not None and fixes.exists()
    gate = threading.Event()
    real_write = rep.write

    def slow_write(path: Any, fmt: str = "csv") -> None:
        gate.wait(20)
        real_write(path, fmt)

    rep.write = slow_write                      # type: ignore[method-assign]
    out = tmp_path / "hisobot.jsonl"
    task = d.save_report(out)
    assert task is not None
    d.close()                                   # the dialog lets the report go ...
    assert d.report is None and fixes.exists()  # ... but the running save still reads it
    gate.set()
    qtbot.waitUntil(lambda: task.state == "done", timeout=TIMEOUT)
    assert len(out.read_text(encoding="utf-8").splitlines()) == 10_020
    assert not fixes.exists() and rep.fixes_file is None
    assert app.statuses[-1] == f"Hisobot yozildi: {out}"


def test_a_result_after_the_dialog_closed_is_closed(qtbot, app, dialogs, tmp_path, monkeypatch, yes) -> None:
    src = write(tmp_path / "nan.json", many_nans(10_020))
    gate = threading.Event()
    reports: list = []
    real = rd.repair_file

    def slow(*args: Any, **kw: Any) -> Any:
        gate.wait(20)
        rep = real(*args, **{**kw, "cancel": None})   # finishes although the dialog asked it to stop
        reports.append(rep)
        return rep

    monkeypatch.setattr(rd, "repair_file", slow)
    d = dialogs(src)
    task = d.check()
    assert task is not None and d.running
    # declining keeps the window open and the run going
    monkeypatch.setattr(widgets, "confirm_handler", lambda *_a: False)
    d.close()
    assert d.isVisible() and d.running
    monkeypatch.setattr(widgets, "confirm_handler", lambda *_a: True)
    d.close()
    assert not d.isVisible() and task.cancel_requested
    gate.set()
    qtbot.waitUntil(lambda: task.state != "running", timeout=TIMEOUT)
    assert reports and reports[0].fixes_file is None          # closed by the job, nobody shows it
    assert not [p for p in (tmp_path).iterdir() if p.name.startswith("nan.repaired")]


def test_cancel_and_progress(qtbot, app, dialogs, tmp_path, monkeypatch) -> None:
    src = write(tmp_path / "buzuq.json", BROKEN)
    reached = threading.Event()

    def slow(source: Any, target: Any, opts: Any = None, *, progress: Any = None, cancel: Any = None,
             **_kw: Any) -> Any:
        progress.set_total(4 << 20)
        progress.set_message("Ta'mirlanmoqda: buzuq.json")
        progress.update(1 << 20)
        reached.set()
        while not cancel.cancelled:
            time.sleep(0.005)
        raise Cancelled()

    monkeypatch.setattr(rd, "repair_file", slow)
    d = dialogs(src)
    cancelled: list = []
    d.run_cancelled.connect(lambda: cancelled.append(True))
    task = d.repair()
    qtbot.waitUntil(reached.is_set, timeout=TIMEOUT)
    assert d.running and d.stop_button.isVisible() and not d.check_button.isEnabled()
    assert not d.options_box.isEnabled() and not d.source_edit.isEnabled()
    qtbot.waitUntil(lambda: d.progress_bar.value() == 250, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: f"{format_bytes(1 << 20)} / {format_bytes(4 << 20)}" in d.status_text(),
                    timeout=TIMEOUT)
    assert d.status_text().startswith("Ta'mirlanmoqda: buzuq.json — ")
    d.cancel_run()
    qtbot.waitUntil(lambda: bool(cancelled), timeout=TIMEOUT)
    assert task.state == "cancelled" and d.status_text() == "To'xtatildi; hech narsa yozilmadi."
    assert not d.running and d.check_button.isEnabled() and not (tmp_path / "buzuq.repaired.json").exists()


def test_the_target_is_never_the_source(qtbot, app, dialogs, tmp_path, yes) -> None:
    src = write(tmp_path / "buzuq.json", BROKEN)
    d = dialogs(src)
    spellings = [src, Path(str(src).upper()), tmp_path / "." / "buzuq.json",
                 tmp_path / "sub" / ".." / "buzuq.json"]
    link = tmp_path / "boshqa_nom.json"
    try:
        os.link(src, link)
        spellings.append(link)
    except OSError:  # pragma: no cover - a file system without hard links
        pass
    for target in spellings:
        d.set_target_path(target)
        assert d.repair() is None, target
        assert "boshqa nom tanlang" in d.error_text() and not d.running
    assert src.read_bytes() == BROKEN
    # the core refuses it as well (a second line of defence)
    with pytest.raises(Exception) as info:
        repair_file(src, Path(str(src).upper()))
    assert getattr(info.value, "key", "") == "writer.error.same_file"
    # no target / a folder
    d.set_target_path("")
    assert d.repair() is None and d.error_text() == t("repair.error.no_target")
    d.set_target_path(tmp_path)
    assert d.repair() is None and "papka" in d.error_text()
    assert not app.errors


def test_inputs_options_and_errors(qtbot, app, dialogs, tmp_path, yes) -> None:
    assert default_target("C:/d/a.json") == str(Path("C:/d/a.repaired.json"))
    assert default_target("C:/d/a.json.gz") == str(Path("C:/d/a.repaired.json.gz"))
    assert default_target("C:/d/a") == str(Path("C:/d/a.repaired.json"))
    d = dialogs(None, hints={"format": "stream", "record_path": ("data", "items")})
    assert d.windowTitle() == "JSON'ni ta'mirlash" and not d.check_button.isEnabled()
    assert d.check() is None and d.error_text() == t("gui.repairdialog.source.need")
    src = write(tmp_path / "oqim.json", b'{"a": 1}{"b": NaN}')
    picked: list = []
    d._ask_open = lambda *a: (picked.append(a), str(src))[1]
    d.choose_source()
    assert d.source_path() == str(src) and d.target_path() == str(tmp_path / "oqim.repaired.json")
    assert d.windowTitle() == "JSON'ni ta'mirlash — oqim.json"
    opts = d.options()
    assert opts.format == "stream" and opts.record_path == ("data", "items") and opts.nan == "null"
    d.record_path_edit.setText("data[")
    assert d.check() is None and d.error_text().startswith("Yozuvlar yo'li noto'g'ri")
    d.record_path_edit.setText("")
    d.nan_combo.setCurrentIndex(1)
    d.jsonl_check.setChecked(True)
    assert d.options() == RepairOptions(nan="string", format="stream", output_kind="jsonl")
    d._ask_save = lambda *a: str(tmp_path / "chiqish.jsonl")
    d.choose_target()
    rep = finish(qtbot, d, d.repair)
    assert (tmp_path / "chiqish.jsonl").read_bytes() == b'{"a": 1}\n{"b": "NaN"}\n'
    assert rep.output_kind == "jsonl"
    # the source file follows no longer changes a target the user chose
    d.set_source_path(src)
    assert d.target_path() == str(tmp_path / "chiqish.jsonl")
    # a JSON Lines output for an array is refused by the core: shown inline
    arr = write(tmp_path / "massiv.json", b"[1, NaN]")
    d.set_source_path(arr)
    d.format_combo.setCurrentIndex(0)
    d.set_target_path(tmp_path / "m.json")
    assert d.repair() is not None
    qtbot.waitUntil(lambda: not d.running, timeout=TIMEOUT)
    assert "JSON Lines" in d.error_text() and not (tmp_path / "m.json").exists()
    # a missing source
    d.jsonl_check.setChecked(False)
    d.set_source_path(tmp_path / "yoq.json")
    assert d.check() is not None
    qtbot.waitUntil(lambda: not d.running, timeout=TIMEOUT)
    assert "yoq.json" in d.error_text() and d.report is None and not app.errors


def test_a_valid_file_has_no_problems(qtbot, app, dialogs, tmp_path) -> None:
    src = write(tmp_path / "toza.json", VALID)
    d = dialogs(src)
    finish(qtbot, d, d.check)
    assert d.headline_text().endswith("Muammo topilmadi.") and d.summary_row_count() == 0
    assert not d.save_report_button.isEnabled() and not d.data_label.isVisible()
    rep = finish(qtbot, d, d.repair)
    assert d.headline_text().endswith("Muammo topilmadi; o'zgarishsiz nusxa yozildi.")
    assert (tmp_path / "toza.repaired.json").read_bytes() == VALID and rep.written


def test_show_repair_dialog_reuses_the_window(qtbot, app, tmp_path) -> None:
    src = write(tmp_path / "buzuq.json", BROKEN)
    d1 = rd.show_repair_dialog(app, src)
    qtbot.waitExposed(d1)
    assert not d1.isModal() and d1.isVisible()
    assert rd.show_repair_dialog(app, str(src).upper() if os.name == "nt" else src) is d1
    other = rd.show_repair_dialog(app, write(tmp_path / "boshqa.json", BROKEN))
    assert other is not d1
    other.close()
    d1.close()


def test_uzbek_and_english_texts(qtbot, app, dialogs, tmp_path) -> None:
    from jsonreshape import i18n

    src = write(tmp_path / "buzuq.json", BROKEN)
    d = dialogs(src)
    assert d.check_button.text() == "Tekshirish" and d.repair_button.text() == "Ta'mirlash"
    assert d.save_report_button.text() == "Hisobotni saqlash…" and d.open_button.text() == "Ta'mirlangan faylni ochish"
    assert [c.title for c in d.summary_grid.columns()] == ["Turi", "Tuzatish", "Soni", "Birinchi joyi"]
    assert d.nan_combo.itemText(0) == "null ga almashtirish"
    keys = mnemonics(d)                            # keyboard: every Alt+letter is used once
    assert len(keys) >= 4 and len(keys) == len(set(keys)), keys
    old = i18n.get_language()
    i18n.set_language("en")
    try:
        e = dialogs(src)
        assert e.check_button.text() == "Check" and e.repair_button.text() == "Repair"
        assert [c.title for c in e.fixes_grid.columns()] == ["Fix", "Line", "Column", "Before", "After", "Record"]
        keys = mnemonics(e)
        assert len(keys) >= 4 and len(keys) == len(set(keys)), keys
        finish(qtbot, e, e.check)
        assert e.headline_text().startswith("Checked: buzuq.json")
        assert cell(e.summary_grid, 0, "first").text.startswith("line ")
    finally:
        i18n.set_language(old)
