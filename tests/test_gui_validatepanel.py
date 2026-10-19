"""JSON Schema in the GUI: jsonreshape/gui/validatepanel.py (docs/ARCHITECTURE.md §14.3, §14.9; s5/map_stats.md §8.7).

Generation (cached full analysis / full scan / sample with its warning, document or record level, strict, title,
the source file refused, stop, open the result) and validation (issues listed in the grid exactly as
``jschema.validate`` reports them, streaming through an ``on_issues`` hook, activation -> reveal, ``record=None``
issues, "show the invalid records", CSV / JSON Lines export of every issue, the summary, cancel, the stale /
outdated rules after ``data_changed``, format assertion on / off, the issue limits, schema errors inline, the
current view as the source, both core backends, Uzbek and English texts).
"""

from __future__ import annotations

import csv
import io
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from jsonreshape import codec, jschema, reader
from jsonreshape.errors import Cancelled
from jsonreshape.gui import fields, widgets
from jsonreshape.gui import validatepanel as vp
from jsonreshape.gui.context import AppContext
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.session import DocSession, FilterSpec, NodeRef
from jsonreshape.gui.validatepanel import GenerateSchemaDialog, ValidateDialog, ValidatePanel, issue_ref
from jsonreshape.i18n import t

pytestmark = pytest.mark.gui

TIMEOUT = 30000
ROOT = Path(__file__).resolve().parents[1]

# JSON text: the exact maximum 1e400 (above every double) must not become a float infinity
SCHEMA_TEXT = """{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Odamlar",
  "type": "object",
  "properties": {
    "id": {"type": "integer"},
    "name": {"type": "string", "minLength": 1},
    "age": {"type": "integer", "minimum": 0},
    "email": {"type": "string", "format": "email"},
    "score": {"type": "number", "maximum": 1e400}
  },
  "required": ["id", "name"]
}"""
SCHEMA = codec.loads(SCHEMA_TEXT)


# ----------------------------------------------------------------------------------------
# data
# ----------------------------------------------------------------------------------------
def person(i: int) -> dict[str, Any]:
    rec: dict[str, Any] = {"id": i, "name": ["Ali", "Oʻgʻiloy", "Самарқанд", "Ma'ruf"][i % 4] + f" {i}"}
    if i % 10 == 3:
        del rec["name"]                         # required
    rec["age"] = -1 if i % 7 == 5 else 20 + i   # minimum
    rec["email"] = "yaroqsiz" if i % 4 == 0 else f"u{i}@example.com"   # format (asserted only on request)
    return rec


def write_people(path: Path, n: int = 40) -> Path:
    # exact number texts stay in the file (1.10, 1e400, -0.0, 12345678901234567890)
    lines = []
    for i in range(n):
        text = json.dumps(person(i), ensure_ascii=False)
        extra = ["1.10", "-0.0", "12345678901234567890", "0.1"][i % 4]
        lines.append(text[:-1] + f', "score": {extra}}}')
    lines.append('{"id": 999, "name": "katta", "age": 1, "email": "k@example.com", "score": 1e400}')
    path.write_text("[\n" + ",\n".join(lines) + "\n]", encoding="utf-8")
    return path


def write_schema(path: Path, value: Any = None) -> Path:
    if value is None:
        path.write_text(SCHEMA_TEXT, encoding="utf-8")
    else:
        path.write_bytes(jschema.dump_schema(value))
    return path


@pytest.fixture
def backend(core_backend, monkeypatch):
    """Documents use this core backend's Scanner; codec.loads its plain-number check."""
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    monkeypatch.setattr(codec, "_fast_checker", core_backend.numbers_plain if name == "rust" else False)
    yield name
    reader.set_scanner_backend(old)


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
def opened(qtbot, app):
    made: list[DocSession] = []

    def factory(path: Path, *, edit: bool = False, **kw: Any) -> DocSession:
        kw.setdefault("use_cache", False)
        s = DocSession(app, path, **kw)
        made.append(s)
        with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
            s.start()
        assert s.state == "ready", s.open_error
        if edit:
            qtbot.waitUntil(lambda: s.edit is not None and s.edit.available, timeout=TIMEOUT)
        return s

    yield factory
    for s in made:
        s.close()
    assert app.tasks.wait_idle(30)


@pytest.fixture
def panels(qtbot, app):
    made: list[ValidatePanel] = []

    def make(session: DocSession | None = None, **kw: Any) -> ValidatePanel:
        p = ValidatePanel(app, **kw)
        qtbot.addWidget(p)
        p.resize(1000, 640)
        p.show()
        if session is not None:
            p.set_session(session)
        made.append(p)
        return p

    yield make
    for p in made:
        p.shutdown()


@pytest.fixture
def yes(monkeypatch):
    monkeypatch.setattr(widgets, "confirm_handler", lambda *_a: True)


def run(qtbot, panel: ValidatePanel) -> str:
    with qtbot.waitSignal(panel.validation_finished, timeout=TIMEOUT) as blocker:
        assert panel.run_validation() is not None, panel.error_text()
    return blocker.args[1]


def oracle(s: DocSession, schema_path: Path, **kw: Any) -> Any:
    return jschema.validate(s.doc, jschema.load_schema(schema_path), **kw)


def keyed(issues: list) -> list[tuple]:
    return [(i.record, i.key, tuple(i.path), i.keyword, i.schema_path, dict(i.params)) for i in issues]


def cell_text(panel: ValidatePanel, row: int, column_id: str) -> str:
    column = next(c for c in panel.grid.columns() if c.id == column_id)
    cell = panel.grid.source().cell(row, column)
    return cell.text if cell is not None else ""


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
# validation: results
# ----------------------------------------------------------------------------------------
def test_issues_listed_exactly_as_the_core_reports_them(qtbot, app, opened, panels, tmp_path, backend) -> None:
    s = opened(write_people(tmp_path / "odamlar.json"))
    schema = write_schema(tmp_path / "odamlar.schema.json")
    p = panels(s)
    with qtbot.waitSignal(p.schema_checked, timeout=TIMEOUT) as checked:
        p.set_schema_path(schema)
    assert checked.args[0] is True and "Odamlar" in p.schema_info_text()
    assert run(qtbot, p) == "done"
    want = oracle(s, schema)
    try:
        st = p.state()
        assert keyed(st.issues) == keyed(want.issues) and len(st.issues) == 9
        assert st.report.invalid == want.invalid == 8 and st.report.scanned == 41
        assert sorted({i.record for i in st.issues}) == [3, 5, 12, 13, 19, 23, 26, 33]
        assert p.issue_count_shown() == 9
        # the grid: 0-based record numbers, the path, the localized message, the keyword
        row = next(r for r, i in enumerate(st.issues) if i.keyword == "minimum")
        issue = st.issues[row]
        assert cell_text(p, row, "record") == f"#{issue.record}"
        assert cell_text(p, row, "path") == "age"
        assert cell_text(p, row, "message") == issue.message and "-1" in issue.message
        assert cell_text(p, row, "keyword") == "minimum"
        req = next(r for r, i in enumerate(st.issues) if i.keyword == "required")
        assert cell_text(p, req, "path") == "$"
        summary = p.summary_text()
        assert "41 ta yozuv tekshirildi: 8 tasi schemaga mos emas, jami 9 ta xato" in summary
        assert "Schema har bir yozuvga qo'llandi." in summary
        assert "minimum (5)" in summary and "required (4)" in summary
        assert "age (5)" in summary
        assert p.records_button.isEnabled() and not app.errors
    finally:
        want.close()


def test_activation_reveals_the_node_of_the_issue(qtbot, app, opened, panels, tmp_path) -> None:
    s = opened(write_people(tmp_path / "odamlar.json"))
    p = panels(s)
    p.set_schema_path(write_schema(tmp_path / "s.json"))
    run(qtbot, p)
    revealed: list = []
    s.reveal_requested.connect(revealed.append)
    row = next(r for r in range(p.issue_count_shown()) if p.issue_at(r).keyword == "minimum")
    issue = p.issue_at(row)
    ref = p.activate_issue(row)
    assert ref == NodeRef(issue.record, ("age",)) and revealed == [ref] and s.current == ref
    # Enter on the grid row does the same
    other = next(r for r in range(p.issue_count_shown()) if p.issue_at(r).keyword == "required")
    p.grid.set_current(other, 0)
    p.grid.setFocus()
    qtbot.keyClick(p.grid, "\r")
    assert revealed[-1] == NodeRef(p.issue_at(other).record, ())
    # the context menu offers the same action
    menu = p.issue_menu(row)
    names = [a.objectName() for a in menu.actions() if a.objectName()]
    assert names == ["show_in_tree", "copy_message", "copy_path", "copy_rows"]
    assert p.copy_rows([row]).split("\t")[:4] == [str(issue.record), "", "age", "minimum"]


def test_single_document_and_document_level_issues(qtbot, app, opened, panels, tmp_path) -> None:
    # a single-value document: record 0 is the document itself -> NodeRef(None, path)
    single = tmp_path / "bitta.json"
    single.write_text('{"a": "x", "b": [1, 2]}', encoding="utf-8")
    s1 = opened(single)
    schema = write_schema(tmp_path / "obj.json", {"type": "object", "properties": {"a": {"type": "integer"}}})
    p = panels(s1)
    p.set_schema_path(schema)
    run(qtbot, p)
    assert [(i.record, i.path) for i in p.state().issues] == [(0, ("a",))]
    assert not p.source_combo.isEnabled()
    assert p.activate_issue(0) == NodeRef(None, ("a",))
    ok, why = p._can_show_records(p.state())
    assert not ok and why == t("gui.validatepanel.records.single")

    # an array with a document schema: minItems concerns the record container (no node to show)
    arr = tmp_path / "massiv.json"
    arr.write_text('[{"a": 1}, {"a": "b"}]', encoding="utf-8")
    s2 = opened(arr)
    doc_schema = write_schema(tmp_path / "doc.json", {"type": "array", "minItems": 5,
                                                      "items": {"type": "object",
                                                                "properties": {"a": {"type": "integer"}}}})
    p.set_session(s2)
    p.set_schema_path(doc_schema)
    run(qtbot, p)
    issues = p.state().issues
    # the records' issues first, the container's aggregates (minItems) after the record pass
    assert [(i.record, i.path, i.keyword) for i in issues] == [(1, ("a",), "type"), (None, (), "minItems")]
    assert cell_text(p, 1, "record") == "hujjat"
    revealed: list = []
    s2.reveal_requested.connect(revealed.append)
    assert p.activate_issue(1) is None and revealed == []
    assert p.note_text() == t("gui.validatepanel.whole_document")
    assert p.activate_issue(0) == NodeRef(1, ("a",)) and p.note_text() == ""
    assert "Schema butun hujjatga qo'llandi." in p.summary_text()
    assert "Hujjatning o'ziga tegishli xatolar: 1" in p.summary_text()

    # a wrapped document: a wrapper issue points into the wrapper (record None, absolute path)
    wrapped = tmp_path / "orama.json"
    wrapped.write_text('{"meta": {"v": "x"}, "data": [{"a": 1}, {"a": "b"}], "tail": true}', encoding="utf-8")
    s3 = opened(wrapped, record_path="data")
    w_schema = write_schema(tmp_path / "w.json", {
        "type": "object",
        "properties": {"meta": {"type": "object", "properties": {"v": {"type": "integer"}}},
                       "data": {"type": "array", "items": {"type": "object",
                                                           "properties": {"a": {"type": "integer"}}}}}})
    p.set_session(s3)
    p.set_schema_path(w_schema)
    run(qtbot, p)
    assert [(i.record, i.path) for i in p.state().issues] == [(None, ("meta", "v")), (1, ("a",))]
    assert issue_ref(s3, p.issue_at(0)) == NodeRef(None, ("meta", "v"))
    assert p.activate_issue(0) == NodeRef(None, ("meta", "v")) and s3.current == NodeRef(None, ("meta", "v"))
    # each session keeps its own results
    p.set_session(s2)
    assert p.issue_count_shown() == 2 and p.issue_at(1).keyword == "minItems"


def test_show_invalid_records_as_the_view(qtbot, app, opened, panels, tmp_path, monkeypatch) -> None:
    from views_edit_helpers import GuiReads

    s = opened(write_people(tmp_path / "odamlar.json"))
    reads = GuiReads(monkeypatch)                # the GUI thread never reads the data (§11.0)
    p = panels(s)
    p.set_schema_path(write_schema(tmp_path / "odamlar.schema.json"))
    run(qtbot, p)
    with qtbot.waitSignal(p.export_finished, timeout=TIMEOUT):
        p.export_issues_to(tmp_path / "x.csv")
    assert reads.big() == [] and not [r for r in reads.reads if r[0] == "read"]
    report = p.state().report
    with qtbot.waitSignal(p.records_opened, timeout=TIMEOUT):
        assert p.show_invalid_records() is not None
    qtbot.waitUntil(lambda: s.view_len() == 8, timeout=TIMEOUT)
    assert [s.record_at(i) for i in range(8)] == [3, 5, 12, 13, 19, 23, 26, 33] == list(report.records)
    assert s.view_label() == "Schemaga mos emas: odamlar.schema.json"
    # the panel's own set is still open (a copy went to the session): it can be shown again
    assert not report.records.closed
    with qtbot.waitSignal(p.records_opened, timeout=TIMEOUT):
        p.show_invalid_records()


def test_export_writes_every_issue(qtbot, app, opened, panels, tmp_path, backend) -> None:
    src = write_people(tmp_path / "odamlar.json")
    s = opened(src)
    schema = write_schema(tmp_path / "s.json")
    p = panels(s)
    p.set_schema_path(schema)
    p.set_max_issues(2)                          # the list is cut, the export is not
    p.set_formats(True)
    run(qtbot, p)
    assert p.issue_count_shown() == 2 and p.state().report.truncated
    every = list(jschema.iter_issues(s.doc, jschema.load_schema(schema), formats=True))
    assert len(every) > 9
    out_csv = tmp_path / "xatolar.csv"
    with qtbot.waitSignal(p.export_finished, timeout=TIMEOUT) as done:
        assert p.export_issues_to(out_csv) is not None, p.error_text()
    assert done.args == [str(out_csv), len(every)]
    text = out_csv.read_bytes()
    assert text.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(text.decode("utf-8-sig"))))
    assert rows[0] == list(vp.EXPORT_FIELDS) and len(rows) == len(every) + 1
    assert [(r[0], r[2], r[3], r[5]) for r in rows[1:]] == [
        (str(i.record), i.path_text, i.keyword, i.message) for i in every]
    out_jsonl = tmp_path / "xatolar.jsonl"
    with qtbot.waitSignal(p.export_finished, timeout=TIMEOUT):
        assert p.export_issues_to(out_jsonl) is not None
    lines = [json.loads(line) for line in out_jsonl.read_text(encoding="utf-8").splitlines()]
    assert lines == [i.to_dict() for i in every]
    # the document and the schema file are never overwritten
    before = src.read_bytes()
    for target in (src, Path(str(src).upper()), schema, tmp_path / "x.txt"):
        assert p.export_issues_to(target) is None
        assert p.error_text()
    assert src.read_bytes() == before and not (tmp_path / "x.txt").exists()


def test_formats_on_and_off(qtbot, app, opened, panels, tmp_path) -> None:
    s = opened(write_people(tmp_path / "odamlar.json"))
    schema = write_schema(tmp_path / "s.json")
    p = panels(s)
    p.set_schema_path(schema)
    run(qtbot, p)
    assert "format" not in {i.keyword for i in p.state().issues}
    p.set_formats(True)
    run(qtbot, p)
    want = oracle(s, schema, formats=True)
    try:
        assert keyed(p.state().issues) == keyed(want.issues)
        fmt = [i for i in p.state().issues if i.keyword == "format"]
        assert len(fmt) == 10 and all(i.path == ("email",) for i in fmt)
        assert "Formatlar ham tekshirildi." in p.summary_text()
    finally:
        want.close()


def test_issue_limits(qtbot, app, opened, panels, tmp_path) -> None:
    s = opened(write_people(tmp_path / "odamlar.json"))
    p = panels(s)
    p.set_schema_path(write_schema(tmp_path / "s.json"))
    p.set_max_issues(3)
    run(qtbot, p)
    rep = p.state().report
    assert p.issue_count_shown() == 3 and rep.issue_count == 9 and rep.truncated
    assert "Ro'yxatda" in p.warnings_text() or "3" in p.warnings_text()
    assert p.warnings_text() == "\n".join(rep.warning_texts()) and p.warnings_label.isVisible()
    p.set_max_issues(1000)
    p.set_max_per_record(1)
    run(qtbot, p)
    rep = p.state().report
    per: dict = {}
    for i in rep.issues:
        per[i.record] = per.get(i.record, 0) + 1
    assert max(per.values()) == 1 and rep.limited_records == 8 and rep.issue_count == 8
    assert p.input_values() == {"schema": p.schema_path(), "scope": "auto", "source": "all", "formats": False,
                                "max_issues": 1000, "per_record": 1}
    # the options are remembered for the next panel
    p2 = ValidatePanel(app)
    qtbot.addWidget(p2)
    try:
        assert p2.max_per_record() == 1 and p2.schema_path() == p.schema_path()
    finally:
        p2.shutdown()


def test_validating_the_current_view(qtbot, app, opened, panels, tmp_path) -> None:
    s = opened(write_people(tmp_path / "odamlar.json"))
    with qtbot.waitSignal(s.view_busy_changed, timeout=TIMEOUT, check_params_cb=lambda busy: not busy):
        s.set_filter(FilterSpec("id < 10"))
    assert s.view_len() == 10
    p = panels(s)
    p.set_schema_path(write_schema(tmp_path / "s.json"))
    p.set_source_choice("view")
    assert "Filtr" in p.source_combo.itemText(1) and "10" in p.source_combo.itemText(1)
    run(qtbot, p)
    rep = p.state().report
    assert rep.scanned == 10 and sorted({i.record for i in rep.issues}) == [3, 5]
    assert "Faqat joriy ko'rinishdagi yozuvlar" in p.summary_text()


def test_schema_errors_are_shown_inline(qtbot, app, opened, panels, tmp_path) -> None:
    s = opened(write_people(tmp_path / "odamlar.json"))
    p = panels(s)
    assert p.run_validation() is None and p.error_text() == t("gui.validatepanel.schema.need")
    bad = tmp_path / "bad.json"
    bad.write_text('{"type": "object", "minimum": "o\'n"}', encoding="utf-8")
    with qtbot.waitSignal(p.schema_checked, timeout=TIMEOUT) as checked:
        p.set_schema_path(bad)
    assert checked.args[0] is False and "minimum" in p.schema_info_text()
    assert run(qtbot, p) == "failed"
    assert "Tekshirib bo'lmadi" in p.summary_text() and "minimum" in p.summary_text()
    broken = tmp_path / "broken.json"
    broken.write_text('{"type": ', encoding="utf-8")
    with qtbot.waitSignal(p.schema_checked, timeout=TIMEOUT) as checked:
        p.set_schema_path(broken)
    assert checked.args[0] is False
    with qtbot.waitSignal(p.schema_checked, timeout=TIMEOUT) as checked:
        p.set_schema_path(tmp_path / "yoq.json")
    assert checked.args[0] is False
    remote = write_schema(tmp_path / "remote.json", {"$ref": "https://example.invalid/x.json"})
    p.set_schema_path(remote)
    assert run(qtbot, p) == "failed" and "https://example.invalid/x.json" in p.summary_text()
    assert not app.errors                       # localized user errors: inline, no error window


# ----------------------------------------------------------------------------------------
# validation: running state
# ----------------------------------------------------------------------------------------
def _blocking_validate(started: threading.Event):
    def fake(doc: Any, compiled: Any, source: Any = None, *, cancel: Any = None, **_kw: Any) -> Any:
        started.set()
        while not cancel.cancelled:
            time.sleep(0.005)
        raise Cancelled()
    return fake


def test_cancel(qtbot, app, opened, panels, tmp_path, monkeypatch) -> None:
    s = opened(write_people(tmp_path / "odamlar.json"))
    p = panels(s)
    p.set_schema_path(write_schema(tmp_path / "s.json"))
    started = threading.Event()
    monkeypatch.setattr(jschema, "validate", _blocking_validate(started))
    assert p.run_validation() is not None
    qtbot.waitUntil(started.is_set, timeout=TIMEOUT)
    assert p.is_running() and p.cancel_button.isVisible() and not p.run_button.isVisible()
    assert p.progress_bar.isVisible()
    with qtbot.waitSignal(p.validation_finished, timeout=TIMEOUT) as blocker:
        p.cancel_validation()
    assert blocker.args[1] == "cancelled" and not p.is_running()
    assert p.summary_text().startswith("To'xtatildi")
    assert not p.records_button.isEnabled() and p.run_button.isVisible()


def test_issues_stream_into_the_grid_while_running(qtbot, app, opened, panels, tmp_path, monkeypatch) -> None:
    s = opened(write_people(tmp_path / "odamlar.json"))
    schema = write_schema(tmp_path / "s.json")
    real = jschema.validate
    gate = threading.Event()
    calls: list[dict] = []

    def streaming(doc: Any, compiled: Any, source: Any = None, *, on_issues: Any = None, **kw: Any) -> Any:
        calls.append(dict(kw))
        every = list(jschema.iter_issues(doc, compiled, source, scope=kw["scope"],
                                         max_per_record=kw["max_per_record"], formats=kw["formats"]))
        on_issues(every[:2])
        gate.wait(20)
        on_issues(every[2:])
        return real(doc, compiled, source, **kw)

    monkeypatch.setattr(jschema, "validate", streaming)
    p = panels(s)
    p.set_schema_path(schema)
    p.set_max_issues(5)
    task = p.run_validation()
    assert task is not None
    qtbot.waitUntil(lambda: p.issue_count_shown() == 2, timeout=TIMEOUT)
    assert p.is_running() and "2 ta xato topildi" in p.summary_text()
    with qtbot.waitSignal(p.validation_finished, timeout=TIMEOUT):
        gate.set()
    assert calls and calls[0]["max_issues"] == 5
    assert p.issue_count_shown() == 5 and p.state().live_count == 9
    assert keyed(p.state().issues) == keyed(p.state().report.issues)


def test_stale_and_outdated_after_edits(qtbot, app, opened, panels, tmp_path, monkeypatch) -> None:
    s = opened(write_people(tmp_path / "odamlar.json"), edit=True)
    p = panels(s)
    p.set_schema_path(write_schema(tmp_path / "s.json"))
    run(qtbot, p)
    assert p.issue_count_shown() == 9
    # a content edit: the list stays (record numbers still valid) but is marked outdated
    changes: list = []
    s.data_changed.connect(changes.append)
    assert s.edit.set_value(NodeRef(0, ("id",)), "100") is not False
    qtbot.waitUntil(lambda: len(changes) == 1 and not s.edit.busy, timeout=TIMEOUT)
    st = p.state()
    assert st.outdated and p.issue_count_shown() == 9 and st.report is not None
    assert t("gui.validatepanel.outdated") in p.summary_text()
    assert not p.records_button.isEnabled()
    assert p.records_button.toolTip() == t("gui.validatepanel.records.outdated")
    assert p.activate_issue(0) is not None
    # records moved: the results are dropped (their numbers address other records now)
    report = st.report
    assert s.edit.delete_records([1]) is not False
    qtbot.waitUntil(lambda: len(changes) == 2 and not s.edit.busy, timeout=TIMEOUT)
    assert p.issue_count_shown() == 0 and st.status == "stale" and st.report is None
    assert report.records.closed
    assert t("gui.validatepanel.stale_edit") in p.summary_text()
    # an edit while a validation runs stops it and drops what it found
    started = threading.Event()
    monkeypatch.setattr(jschema, "validate", _blocking_validate(started))
    task = p.run_validation()
    qtbot.waitUntil(started.is_set, timeout=TIMEOUT)
    assert s.edit.set_value(NodeRef(0, ("id",)), "101") is not False
    qtbot.waitUntil(lambda: len(changes) == 3 and not s.edit.busy, timeout=TIMEOUT)
    assert st.status == "stale" and t("gui.validatepanel.stale_running") in p.summary_text()
    qtbot.waitUntil(lambda: task.state != "running", timeout=TIMEOUT)
    assert task.state == "cancelled"


def test_reopen_and_close_drop_the_results(qtbot, app, opened, panels, tmp_path) -> None:
    s = opened(write_people(tmp_path / "odamlar.json"))
    p = panels(s)
    p.set_schema_path(write_schema(tmp_path / "s.json"))
    run(qtbot, p)
    report = p.state().report
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen()
    assert p.issue_count_shown() == 0 and p.state().status == "stale" and report.records.closed
    assert t("gui.validatepanel.stale") in p.summary_text()
    run(qtbot, p)
    report = p.state().report
    s.close()
    assert p.session() is None and report.records.closed and p.issue_count_shown() == 0
    assert p.summary_text() == t("gui.validatepanel.no_document")


def test_validate_dialog_is_one_window_per_session(qtbot, app, opened, tmp_path) -> None:
    s = opened(write_people(tmp_path / "odamlar.json"))
    schema = write_schema(tmp_path / "s.json")
    d1 = vp.show_validate_dialog(s, schema_path=str(schema))
    qtbot.waitExposed(d1)
    assert d1.windowTitle() == "Schema bo'yicha tekshirish — odamlar.json" and not d1.isModal()
    assert d1.panel.schema_path() == str(schema)
    assert vp.show_validate_dialog(s) is d1
    with qtbot.waitSignal(d1.panel.validation_finished, timeout=TIMEOUT):
        d1.panel.run_validation()
    report = d1.panel.state().report
    d1.close()
    assert d1.panel._shut and report.records.closed
    d2 = ValidateDialog.show_for(s)
    assert d2 is not d1
    d2.close()


# ----------------------------------------------------------------------------------------
# generation
# ----------------------------------------------------------------------------------------
@pytest.fixture
def gen_dialog(qtbot, app):
    made: list[GenerateSchemaDialog] = []

    def make(session: DocSession, **kw: Any) -> GenerateSchemaDialog:
        d = GenerateSchemaDialog(session, **kw)
        qtbot.addWidget(d)
        d.show()
        made.append(d)
        return d

    return make


def generate(qtbot, d: GenerateSchemaDialog, path: Path | None = None) -> Any:
    with qtbot.waitSignal(d.generated, timeout=TIMEOUT) as blocker:
        assert d.generate(path) is not None, d.error_text()
    return blocker.args[0]


def full_schema(qtbot, s: DocSession) -> Any:
    got: list = []
    have = fields.get_schema(s, got.append, full=True)
    if have is None:
        qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
        have = got[0]
    return have


def test_generate_from_the_cached_full_analysis(qtbot, app, opened, gen_dialog, tmp_path) -> None:
    s = opened(write_people(tmp_path / "odamlar.json"))
    d0 = gen_dialog(s)
    assert not d0.source_buttons["cached"].isEnabled() and d0.source_choice() == "full"
    cached = full_schema(qtbot, s)
    d = gen_dialog(s, ask_save_path=lambda *_a: str(tmp_path / "chiqish"))
    assert d.source_buttons["cached"].isEnabled() and d.source_choice() == "cached"
    assert "41" in d.source_buttons["cached"].text()
    assert d.title_text() == "odamlar.json" and d.document_level() and not d.strict()
    outcome = generate(qtbot, d)
    out = tmp_path / "chiqish.json"
    assert outcome.path == str(out) and outcome.source == "cached" and outcome.complete
    assert outcome.scanned == 41 and outcome.size == out.stat().st_size
    want = jschema.generate_schema(cached, doc=s.doc.pinned(), document=True, title="odamlar.json")
    assert out.read_bytes() == jschema.dump_schema(want)
    value = jschema.load_schema(out)
    assert value["$comment"] == jschema.SCOPE_DOCUMENT and value["type"] == "array"
    rep = jschema.validate(s.doc, value)
    try:
        assert rep.ok and rep.scope == "document"
    finally:
        rep.close()
    assert "Schema yozildi" in d.status_text() and d.open_button.isVisible()
    assert app.statuses[-1] == f"Schema yozildi: {out}"
    assert d.open_result() and app.open_calls[-1] == (str(out), {})


def test_generate_full_scan_record_level_strict(qtbot, app, opened, gen_dialog, tmp_path, backend) -> None:
    s = opened(write_people(tmp_path / "odamlar.json"))
    d = gen_dialog(s)
    d.set_source_choice("full")
    d.set_document_level(False)
    d.set_strict(True)
    d.set_title_text("Xodimlar roʻyxati")
    out = tmp_path / "xodim.schema.json"
    outcome = generate(qtbot, d, out)
    assert outcome.source == "full" and outcome.strict and not outcome.document and outcome.complete
    value = jschema.load_schema(out)
    assert value["title"] == "Xodimlar roʻyxati" and value["$comment"] == jschema.SCOPE_RECORD
    assert value["type"] == "object" and value["properties"]["age"]["minimum"] == -1
    assert codec.dumps(value["properties"]["score"]["maximum"]) == b"1e400"
    assert "Xodimlar roʻyxati".encode() in out.read_bytes()   # non-ASCII never \\u-escaped
    rep = jschema.validate(s.doc, value, formats=True)
    try:
        assert rep.ok, [i.message for i in rep.issues]
    finally:
        rep.close()


def test_generate_from_a_sample_warns(qtbot, app, opened, gen_dialog, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app.settings, "sample_size", 5)
    s = opened(write_people(tmp_path / "odamlar.json"))
    d = gen_dialog(s)
    assert not d.sample_warning.isVisible()
    d.set_source_choice("sample")
    assert d.sample_warning.isVisible() and "5" in d.source_buttons["sample"].text()
    assert "namuna" in d.sample_warning.text().lower()
    outcome = generate(qtbot, d, tmp_path / "namuna.json")
    assert outcome.source == "sample" and not outcome.complete and outcome.scanned == 5
    assert outcome.total_records == 41
    assert "namuna asosida (5 / 41 ta yozuv)" in d.status_text()
    value = jschema.load_schema(tmp_path / "namuna.json")
    assert "namuna" in value["description"]


def test_generate_refuses_the_source_file_and_stops(qtbot, app, opened, gen_dialog, tmp_path, monkeypatch) -> None:
    src = write_people(tmp_path / "odamlar.json")
    s = opened(src)
    d = gen_dialog(s)
    before = src.read_bytes()
    for target in (src, Path(str(src).upper()), tmp_path / "." / "odamlar.json"):
        assert d.generate(target) is None
        assert "boshqa nom tanlang" in d.error_text()
    assert src.read_bytes() == before
    started = threading.Event()

    def blocking(doc: Any, source: Any = None, *, cancel: Any = None, **_kw: Any) -> Any:
        started.set()
        while not cancel.cancelled:
            time.sleep(0.005)
        raise Cancelled()

    monkeypatch.setattr(vp, "infer_schema", blocking)
    d.set_source_choice("full")
    out = tmp_path / "x.schema.json"
    task = d.generate(out)
    assert task is not None and d.running and not d.save_button.isEnabled() and d.stop_button.isVisible()
    qtbot.waitUntil(started.is_set, timeout=TIMEOUT)
    d.cancel()
    qtbot.waitUntil(lambda: not d.running, timeout=TIMEOUT)
    assert task.state == "cancelled" and d.status_text() == "To'xtatildi; fayl yozilmadi." and not out.exists()
    assert d.save_button.isEnabled()


def test_generate_goes_on_after_the_dialog_closed(qtbot, app, opened, tmp_path) -> None:
    s = opened(write_people(tmp_path / "odamlar.json"))
    d = vp.show_generate_dialog(s)
    assert vp.show_generate_dialog(s) is d and not d.isModal()
    out = tmp_path / "keyin.schema.json"
    task = d.generate(out)
    d.close()
    qtbot.waitUntil(lambda: task.state == "done", timeout=TIMEOUT)
    assert out.exists() and app.statuses[-1] == f"Schema yozildi: {out}"


# ----------------------------------------------------------------------------------------
# texts
# ----------------------------------------------------------------------------------------
def test_uzbek_and_english_texts(qtbot, app, opened, panels, gen_dialog, tmp_path) -> None:
    from jsonreshape import i18n

    s = opened(write_people(tmp_path / "odamlar.json"))
    p = panels(s)
    assert p.run_button.text() == "Tekshirish" and p.records_button.text() == "Xatoli yozuvlarni ko'rsatish"
    assert [c.title for c in p.grid.columns()] == ["Yozuv", "Yo'l", "Xato", "Kalit so'z"]
    assert p.scope_combo.itemText(0) == "Avtomatik" and p.formats_check.text() == "Formatlarni tekshirish"
    d = gen_dialog(s)
    assert d.windowTitle() == "Schema yaratish — odamlar.json" and d.strict_check.text() == "Qat'iy cheklovlar"
    for widget in (p, d):                          # keyboard: every Alt+letter is used once
        keys = mnemonics(widget)
        assert keys and len(keys) == len(set(keys)), keys
    old = i18n.get_language()
    i18n.set_language("en")
    try:
        p2 = panels(s)
        assert p2.run_button.text() == "Validate" and p2.export_button.text() == "Export…"
        assert [c.title for c in p2.grid.columns()] == ["Record", "Path", "Problem", "Keyword"]
        keys = mnemonics(p2)
        assert keys and len(keys) == len(set(keys)), keys
        p2.set_schema_path(write_schema(tmp_path / "s.json"))
        run(qtbot, p2)
        assert "41 records validated: 8 do not match the schema, 9 problems in all" in p2.summary_text()
        assert {p2.issue_at(r).message for r in range(p2.issue_count_shown())} >= {
            "A required property is missing: \"name\".", "The number is too small: -1. The minimum is 0."}
    finally:
        i18n.set_language(old)
