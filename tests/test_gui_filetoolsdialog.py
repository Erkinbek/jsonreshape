"""SplitDialog / MergeDialog / FileToolsRunner (jsonreshape/gui/filetoolsdialog.py; docs/ARCHITECTURE.md §14.9,
§14.4; s5/map_convert.md §6).

* split by parts / records / size with the live plan (what ``filetools.plan_split`` plans, the part names the
  core writes), stale plan results dropped, the plan made again after an edit; every source (the whole
  document -- edited --, the current view with a filter, a node), formats (byte copy, JSON indent, JSON
  Lines, CSV), name pattern, no-wrapper, drop-broken; refusals (bad sizes, too many parts, folder problems,
  a part that would be the source, read-only parts, node sources of edited / JSON Lines documents);
  progress + stop; the report window and its callbacks; existing parts replaced only after a question;
* merge of files and an open edited tab (its unsaved edits merged as shown), add / remove / reorder (also
  with the keys), JSON / JSON Lines, indent, sorted keys, keep the wrapper, dedupe by a key keeping the first
  / the last copy, partial inputs, the output refused when it is an input, a closed tab read from disk,
  progress + stop, the report;
* both core backends where the core works (split, merge, dedupe); Uzbek texts; the GUI thread reads no data.
"""

from __future__ import annotations

import gzip
import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import Qt

from jsonreshape import codec, dupes, editops, filetools, reader, writer
from jsonreshape.errors import UserInputError
from jsonreshape.gui import filetoolsdialog as ftd
from jsonreshape.gui import widgets
from jsonreshape.gui.context import AppContext
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.session import DocSession, FilterSpec, NodeRef
from jsonreshape.i18n import t
from jsonreshape.reader import Document
from views_edit_helpers import GuiReads, run

pytestmark = pytest.mark.gui

TIMEOUT = 30000
CITIES = ["Toshkent", "Oʻzbekiston", "Самарқанд", "O'zbek \"qo'shtirnoq\""]


# ----------------------------------------------------------------------------------------
# data
# ----------------------------------------------------------------------------------------
def record_text(i: int, src: str = "A") -> str:
    return ('{"id": %d, "src": "%s", "name": "Ism %d", "city": %s, "nums": [1.10, 1e400, 12345678901234567890, '
            '-0.0, 0.1], "nested": {"x": %d}}' % (i, src, i, json.dumps(CITIES[i % 4], ensure_ascii=False), i * 2))


def write_array(path: Path, n: int = 30, start: int = 0, src: str = "A") -> Path:
    path.write_text("[\n" + ",\n".join("  " + record_text(i, src) for i in range(start, start + n)) + "\n]",
                    encoding="utf-8")
    return path


def write_jsonl(path: Path, n: int = 30, start: int = 0, src: str = "B") -> Path:
    path.write_text("".join(record_text(i, src) + "\n" for i in range(start, start + n)), encoding="utf-8")
    return path


def write_wrapped(path: Path, n: int = 20, src: str = "W") -> Path:
    data = ",".join(record_text(i, src) for i in range(n))
    text = '{"meta": {"v": 1.50, "nom": "Ўзбек"}, "data": [' + data + '], "tail": true}'
    path.write_text(text, encoding="utf-8")
    return path


def open_doc(path: Path, **kw: Any) -> Document:
    doc = Document.open(path, **kw)
    doc.build_index()
    return doc


def records_of(path: Path, **kw: Any) -> list[bytes]:
    """The compact records of a file (exact tokens)."""
    doc = open_doc(path, **kw)
    try:
        assert doc.error is None, (path, doc.error)
        return [compact(doc.record_bytes(i)) for i in range(doc.record_count)]
    finally:
        doc.close()


def compact(data: bytes) -> bytes:
    return codec.dumps(codec.loads(data))


def temps(folder: Path) -> list[str]:
    return sorted(n for n in os.listdir(folder) if n.endswith((filetools.PART_SUFFIX, ".tmp")))


# ----------------------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _fast_plans(monkeypatch):
    monkeypatch.setattr(ftd, "PLAN_DELAY_MS", 5)


@pytest.fixture
def backend(core_backend, monkeypatch):
    """Every core user of the dialogs on one backend: indexing (documents opened in the test), the writer's
    Reformatter, the deduplication's PathExtractor, the file tools."""
    name = "rust" if core_backend.__name__.endswith("_rscore") else "python"
    old = reader.set_scanner_backend(name)
    old_ops = editops.set_core_backend(name)
    monkeypatch.setattr(writer, "core", core_backend)
    monkeypatch.setattr(dupes, "_EXTRACTOR", core_backend.PathExtractor)
    monkeypatch.setattr(filetools, "core", core_backend)
    yield name
    reader.set_scanner_backend(old)
    editops.set_core_backend(old_ops)


@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(10)
    ctx.errors = []
    ctx.statuses = []
    ctx.requested = []
    ctx.error_reported.connect(ctx.errors.append)
    ctx.status_message.connect(lambda text, _ms: ctx.statuses.append(text))
    ctx.open_requested.connect(lambda path, _opts: ctx.requested.append(path))
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
        try:
            s.close()
        except RuntimeError:  # pragma: no cover - deleted already
            pass
    assert app.tasks.wait_idle(30)


class Calls:
    """The injected callbacks and the answers to the questions."""

    def __init__(self) -> None:
        self.folders: list[str] = []
        self.files: list[str] = []
        self.questions: list[str] = []
        self.answer = True
        self.folder_answer: str | None = None
        self.files_answer: list[str] = []
        self.output_answer: str | None = None

    def open_folder(self, path: str) -> bool:
        self.folders.append(path)
        return True

    def open_file(self, path: str) -> bool:
        self.files.append(path)
        return True


@pytest.fixture
def calls(monkeypatch) -> Calls:
    c = Calls()

    def confirm(_parent: Any, text: str, _title: str) -> bool:
        c.questions.append(text)
        return c.answer

    monkeypatch.setattr(widgets, "confirm_handler", confirm)
    return c


@pytest.fixture
def split_dialog(qtbot, calls):
    def make(session: DocSession, **kw: Any) -> ftd.SplitDialog:
        kw.setdefault("open_folder", calls.open_folder)
        kw.setdefault("open_file", calls.open_file)
        kw.setdefault("ask_folder", lambda _parent, _title, _start: calls.folder_answer)
        dlg = ftd.SplitDialog(session, **kw)
        qtbot.addWidget(dlg)
        return dlg

    return make


@pytest.fixture
def merge_dialog(qtbot, app, calls):
    def make(open_documents: Any = None, **kw: Any) -> ftd.MergeDialog:
        kw.setdefault("open_folder", calls.open_folder)
        kw.setdefault("open_file", calls.open_file)
        kw.setdefault("ask_files", lambda _parent, _title, _start: list(calls.files_answer))
        kw.setdefault("ask_output", lambda _parent, _title, _start: calls.output_answer)
        dlg = ftd.MergeDialog(app, open_documents, **kw)
        qtbot.addWidget(dlg)
        return dlg

    return make


def wait_plan(qtbot: Any, dlg: ftd.SplitDialog) -> ftd.SplitPlan | None:
    qtbot.waitUntil(lambda: not dlg.plan_pending, timeout=TIMEOUT)
    return dlg.plan


def run_split(qtbot: Any, dlg: ftd.SplitDialog) -> ftd.SplitOutcome:
    plan = wait_plan(qtbot, dlg)
    assert plan is not None, dlg.plan_errors()
    with qtbot.waitSignal(dlg.split_finished, timeout=TIMEOUT) as blocker:
        assert dlg.start_split() is not None, dlg.error_text()
    return blocker.args[0]


def wait_inputs(qtbot: Any, dlg: ftd.MergeDialog) -> None:
    qtbot.waitUntil(lambda: not dlg.probing and all(i.is_tab or i.info is not None or i.error for i in dlg.inputs()),
                    timeout=TIMEOUT)
    dlg._validate()


def run_merge(qtbot: Any, dlg: ftd.MergeDialog) -> ftd.MergeOutcome:
    wait_inputs(qtbot, dlg)
    with qtbot.waitSignal(dlg.merge_finished, timeout=TIMEOUT) as blocker:
        assert dlg.start_merge() is not None, dlg.error_text()
    return blocker.args[0]


# ----------------------------------------------------------------------------------------
# split: modes, the live plan, the names
# ----------------------------------------------------------------------------------------
def test_split_document_by_parts_records_and_size(qtbot, app, opened, split_dialog, tmp_path, backend) -> None:
    src = write_array(tmp_path / "people.json", 30)
    s = opened(src)
    out = tmp_path / "out"
    out.mkdir()
    dlg = split_dialog(s)
    assert dlg.source() == "document" and dlg.mode() == "parts"
    dlg.set_out_dir(out)
    # parts balanced by records: the plan lists every part, named as the core names them
    dlg.set_parts(3, "records")
    plan = wait_plan(qtbot, dlg)
    assert plan is not None, dlg.plan_errors()
    assert plan.ranges == [(0, 10), (10, 20), (20, 30)] and plan.engine == "same" and plan.records == 30
    assert [r[1] for r in dlg.plan_rows()] == ["people_1.json", "people_2.json", "people_3.json"]
    assert [r[2] for r in dlg.plan_rows()] == ["#0 – #9", "#10 – #19", "#20 – #29"]
    assert dlg.run_button.isEnabled()
    outcome = run_split(qtbot, dlg)
    report = outcome.report
    assert [p.name for p in report.files] == ["people_1.json", "people_2.json", "people_3.json"]
    assert [Path(p) for p in plan.names] == report.files
    want = records_of(src)
    assert sum((records_of(p) for p in report.files), []) == want
    assert report.engine == "same" and not outcome.edited
    assert temps(out) == []
    # parts balanced by bytes: what filetools.plan_split plans for the same options
    dlg.set_parts(4, "bytes")
    plan = wait_plan(qtbot, dlg)
    assert plan is not None and plan.ranges == filetools.plan_split(s.doc, dlg.split_options())
    # records per part
    dlg.set_records(7)
    plan = wait_plan(qtbot, dlg)
    assert [b - a for a, b in plan.ranges] == [7, 7, 7, 7, 2]
    assert [r[3] for r in dlg.plan_rows()] == ["7", "7", "7", "7", "2"]
    # a part size: every part fits (the size counts the head and tail too)
    size = (src.stat().st_size // 3) + 10
    dlg.set_size_text(f"{size / 1024:.3f} KB".replace(".", ","))     # a decimal comma is a decimal point
    plan = wait_plan(qtbot, dlg)
    assert plan is not None, dlg.plan_errors()
    opts = dlg.split_options()
    assert abs(opts.size - size) <= 1
    assert plan.ranges == filetools.plan_split(s.doc, opts)
    outcome = run_split(qtbot, dlg)
    assert all(p.stat().st_size <= opts.size for p in outcome.report.files)
    assert sum((records_of(p) for p in outcome.report.files), []) == want
    assert not app.errors


def test_stale_plan_results_are_dropped(qtbot, app, opened, split_dialog, tmp_path, monkeypatch) -> None:
    s = opened(write_array(tmp_path / "people.json", 30))
    dlg = split_dialog(s)
    wait_plan(qtbot, dlg)
    real = filetools.plan_split
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def slow_plan(doc: Any, opts: Any, source: Any = None, *, cancel: Any = None) -> Any:
        calls.append(opts.parts)
        if len(calls) == 1:
            entered.set()
            release.wait(10)               # ignores the cancel request: its late result must be dropped
        return real(doc, opts, source)

    monkeypatch.setattr(filetools, "plan_split", slow_plan)
    ready: list[Any] = []
    dlg.plan_ready.connect(ready.append)
    dlg.set_parts(2, "records")
    first = dlg.plan_now()
    qtbot.waitUntil(entered.is_set, timeout=TIMEOUT)
    assert dlg.plan is None and dlg.plan_pending and not dlg.run_button.isEnabled()
    assert dlg.plan_label.text() == t("gui.filetools.split.plan.pending")
    dlg.set_parts(3, "records")             # a newer request: the first task is cancelled
    assert first.cancel_requested
    plan = wait_plan(qtbot, dlg)
    assert plan is not None and plan.count == 3
    release.set()
    qtbot.waitUntil(lambda: first.state != "running", timeout=TIMEOUT)
    app.tasks.wait_idle(5)
    assert dlg.plan is plan and plan.count == 3 and [p.count for p in ready] == [3]
    assert calls[:2] == [2, 3]


def test_plan_is_made_again_after_an_edit_and_the_split_writes_the_edits(qtbot, app, opened, split_dialog,
                                                                          tmp_path) -> None:
    s = opened(write_array(tmp_path / "people.json", 25), edit=True)
    out = tmp_path / "out"
    out.mkdir()
    dlg = split_dialog(s)
    dlg.set_out_dir(out)
    dlg.set_records(10)
    plan = wait_plan(qtbot, dlg)
    assert plan.count == 3 and plan.records == 25 and dlg.note_text() == ""
    stale: list[str] = []
    s.data_changed.connect(lambda _cs: stale.append(dlg.plan_label.text()))
    run(qtbot, s.edit.delete_records, [0, 1, 2, 3, 4, 5])
    assert stale and stale[-1] == t("gui.filetools.split.plan.stale")
    plan = wait_plan(qtbot, dlg)
    assert plan is not None and plan.records == 19 and plan.count == 2
    assert dlg.note_text() == t("gui.filetools.split.edited_note")
    run(qtbot, s.edit.set_value, NodeRef(0, ("name",)), '"Tahrirlangan Ўзбек"')
    plan = wait_plan(qtbot, dlg)
    assert plan is not None and plan.gen == s.doc.gen
    outcome = run_split(qtbot, dlg)
    assert outcome.edited and t("gui.filetools.report.edited") in dlg.report_dialog.summary_text()
    pinned = s.doc.pinned()
    want = [compact(pinned.record_bytes(i)) for i in range(pinned.record_count)]
    got = sum((records_of(p) for p in outcome.report.files), [])
    assert got == want and b"Tahrirlangan" in got[0]
    assert records_of(tmp_path / "people.json")[0] != got[0]      # the file itself is unchanged


# ----------------------------------------------------------------------------------------
# split: sources and formats
# ----------------------------------------------------------------------------------------
def test_split_the_filtered_view_to_json_and_csv(qtbot, app, opened, split_dialog, tmp_path, backend) -> None:
    src = write_array(tmp_path / "people.json", 30)
    s = opened(src)
    s.set_filter(FilterSpec("id >= 10 and id < 25"))
    qtbot.waitUntil(lambda: not s.view_busy and not s.view_is_base and s.view_len() == 15, timeout=TIMEOUT)
    out = tmp_path / "out"
    out.mkdir()
    dlg = split_dialog(s)
    assert dlg.source() == "view"
    assert "15" in dlg.source_buttons["view"].text()
    dlg.set_out_dir(out)
    dlg.set_parts(2, "records")
    plan = wait_plan(qtbot, dlg)
    assert plan.engine == "export" and plan.fmt == "json" and plan.records == 15
    outcome = run_split(qtbot, dlg)
    all_records = records_of(src)
    assert sum((records_of(p) for p in outcome.report.files), []) == all_records[10:25]
    # CSV parts share one header
    dlg.set_format("csv")
    plan = wait_plan(qtbot, dlg)
    assert [Path(p).suffix for p in plan.names] == [".csv", ".csv"]
    outcome = run_split(qtbot, dlg)
    texts = [p.read_text(encoding="utf-8-sig").splitlines() for p in outcome.report.files]
    assert texts[0][0] == texts[1][0] and texts[0][0].startswith("id,src,name,city")
    assert len(texts[0]) - 1 + len(texts[1]) - 1 == 15
    # the whole document from the same dialog
    dlg.set_source("document")
    dlg.set_format("same")
    plan = wait_plan(qtbot, dlg)
    assert plan.records == 30 and plan.engine == "same"


def test_split_a_node(qtbot, app, opened, split_dialog, tmp_path) -> None:
    items = [{"n": i, "v": 1.10, "s": "Ўзбек"} for i in range(40)]
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"title": "Hisobot", "groups": [{"name": "g0", "items": items},
                                                              {"name": "g1", "items": [1]}]}, ensure_ascii=False),
                    encoding="utf-8")
    s = opened(path)
    assert s.doc.kind == "wrapped"
    out = tmp_path / "out"
    out.mkdir()
    ref = NodeRef(0, ("items",))
    dlg = split_dialog(s, source=ref)
    assert dlg.source() == "node" and not dlg.source_buttons["node"].isHidden()
    assert "#0.items" in dlg.source_buttons["node"].text()
    assert not dlg.keep_wrapper_check.isChecked() and dlg.keep_wrapper_check.isEnabled()
    dlg.set_out_dir(out)
    dlg.set_parts(4, "records")
    plan = wait_plan(qtbot, dlg)
    assert plan is not None, dlg.plan_errors()
    assert plan.records == 40 and plan.count == 4 and plan.engine == "same"
    outcome = run_split(qtbot, dlg)
    got = []
    for p in outcome.report.files:
        value = json.loads(p.read_text(encoding="utf-8"))
        assert isinstance(value, list) and len(value) == 10          # the elements alone (no wrapper)
        got += value
    assert got == items
    # keep the wrapper: every part is the whole document with a slice of the node
    dlg.keep_wrapper_check.setChecked(True)
    dlg.set_name_pattern("{stem}-w{n}{ext}")
    outcome = run_split(qtbot, dlg)
    first = json.loads(outcome.report.files[0].read_text(encoding="utf-8"))
    assert first["title"] == "Hisobot" and first["groups"][0]["items"] == items[:10]
    # a scalar node is refused by the plan
    dlg.set_source(NodeRef(0, ("name",)))
    assert wait_plan(qtbot, dlg) is None
    assert t("gui.filetools.error.node_scalar", path="#0.name") in dlg.error_text()
    assert not dlg.run_button.isEnabled()


def test_node_source_refused_for_lines_and_edited_documents(qtbot, app, opened, split_dialog, tmp_path) -> None:
    lines = opened(write_jsonl(tmp_path / "lines.jsonl", 5))
    dlg = split_dialog(lines, source=NodeRef(1, ("nums",)))
    assert wait_plan(qtbot, dlg) is None
    assert UserInputError("gui.filetools.error.node_kind", kind=t("reader.kind.jsonl")).message in dlg.error_text()
    s = opened(write_wrapped(tmp_path / "w.json", 5), edit=True)
    run(qtbot, s.edit.set_value, NodeRef(0, ("name",)), '"x"')
    dlg2 = split_dialog(s, source=NodeRef(None, ("data",)))
    assert wait_plan(qtbot, dlg2) is None
    assert t("gui.filetools.error.node_edited") in dlg2.error_text()
    dlg2.set_source("document")                 # the whole (edited) document is fine
    assert wait_plan(qtbot, dlg2) is not None


def test_formats_indent_and_name_pattern(qtbot, app, opened, split_dialog, tmp_path) -> None:
    src = write_array(tmp_path / "people.json", 12)
    s = opened(src)
    out = tmp_path / "out"
    out.mkdir()
    dlg = split_dialog(s)
    dlg.set_out_dir(out)
    dlg.set_parts(2, "records")
    dlg.set_format("jsonl")
    dlg.set_name_pattern("{stem}-qism{n}{ext}")
    plan = wait_plan(qtbot, dlg)
    assert [p.name for p in plan.names] == ["people-qism1.jsonl", "people-qism2.jsonl"]
    assert dlg.indent_combo.isHidden() or not dlg.indent_combo.isEnabled()
    outcome = run_split(qtbot, dlg)
    lines = outcome.report.files[0].read_bytes().splitlines()
    assert len(lines) == 6 and b"1e400" in lines[0] and "Самарқанд".encode() in lines[2]
    # JSON with a tab indentation
    dlg.set_format("json")
    assert dlg.indent_combo.isEnabled()
    ftd._select_data(dlg.indent_combo, "\t")
    outcome = run_split(qtbot, dlg)
    text = outcome.report.files[0].read_text(encoding="utf-8")
    assert text.startswith("[\n\t{\n\t\t\"id\": 0,") and "12345678901234567890" in text
    # a pattern without {n} is refused at once (nothing planned)
    dlg.set_name_pattern("{stem}{ext}")
    assert wait_plan(qtbot, dlg) is None
    assert UserInputError("filetools.error.name_pattern", pattern="{stem}{ext}").message in dlg.error_text()
    assert not dlg.run_button.isEnabled()


def test_single_and_partial_documents(qtbot, app, opened, split_dialog, tmp_path) -> None:
    path = tmp_path / "one.json"
    path.write_text('{"a": 1, "b": {"c1": 1, "c2": 2.50, "c3": "Ўзбек", "c4": [4]}}', encoding="utf-8")
    s = opened(path)
    assert s.doc.kind == "single"
    out = tmp_path / "out"
    out.mkdir()
    dlg = split_dialog(s)
    dlg.set_out_dir(out)
    dlg.set_parts(2, "records")
    assert wait_plan(qtbot, dlg) is None
    assert UserInputError("filetools.error.single").message in dlg.error_text()
    assert t("gui.filetools.hint.single") in dlg.error_text() and "--record-path" not in dlg.error_text()
    # the object inside it, split as a node: its members, two in every part, tokens as they are
    dlg.set_source(NodeRef(None, ("b",)))
    outcome = run_split(qtbot, dlg)
    parts = [p.read_text(encoding="utf-8") for p in outcome.report.files]
    assert [json.loads(x) for x in parts] == [{"c1": 1, "c2": 2.5}, {"c3": "Ўзбек", "c4": [4]}]
    assert '"c2": 2.50' in parts[0]
    bad = tmp_path / "bad.json"
    bad.write_text('[1, 2, {"x": ]', encoding="utf-8")
    sb = opened(bad)
    dlg2 = split_dialog(sb)
    assert wait_plan(qtbot, dlg2) is None
    assert t("gui.filetools.hint.partial") in dlg2.error_text() and "jsonreshape repair" not in dlg2.error_text()


def test_gzip_source_names(qtbot, app, opened, split_dialog, tmp_path) -> None:
    path = tmp_path / "people.json.gz"
    path.write_bytes(gzip.compress(("[" + ",".join(record_text(i) for i in range(6)) + "]").encode("utf-8")))
    s = opened(path)
    out = tmp_path / "out"
    out.mkdir()
    dlg = split_dialog(s)
    dlg.set_out_dir(out)
    dlg.set_parts(2, "records")
    plan = wait_plan(qtbot, dlg)
    assert [p.name for p in plan.names] == ["people_1.json.gz", "people_2.json.gz"]
    outcome = run_split(qtbot, dlg)
    assert outcome.report.files == plan.names
    assert json.loads(gzip.decompress(outcome.report.files[1].read_bytes()))[0]["id"] == 3
    for fmt, names in (("jsonl", ["people_1.jsonl.gz", "people_2.jsonl.gz"]),
                       ("xlsx", ["people_1.xlsx", "people_2.xlsx"])):
        dlg.set_format(fmt)
        assert [p.name for p in wait_plan(qtbot, dlg).names] == names


def test_keep_wrapper_or_not(qtbot, app, opened, split_dialog, tmp_path) -> None:
    s = opened(write_wrapped(tmp_path / "w.json", 10))
    out = tmp_path / "out"
    out.mkdir()
    dlg = split_dialog(s)
    assert dlg.keep_wrapper_check.isEnabled() and dlg.keep_wrapper_check.isChecked()
    dlg.set_out_dir(out)
    dlg.set_parts(2, "records")
    outcome = run_split(qtbot, dlg)
    part = json.loads(outcome.report.files[0].read_text(encoding="utf-8"))
    assert part["meta"]["nom"] == "Ўзбек" and len(part["data"]) == 5 and part["tail"] is True
    dlg.keep_wrapper_check.setChecked(False)
    outcome = run_split(qtbot, dlg)
    part = json.loads(outcome.report.files[1].read_text(encoding="utf-8"))
    assert isinstance(part, list) and [r["id"] for r in part] == [5, 6, 7, 8, 9]
    plain = opened(write_array(tmp_path / "plain.json", 4))
    assert not split_dialog(plain).keep_wrapper_check.isEnabled()


def test_broken_lines_need_drop_broken(qtbot, app, opened, split_dialog, tmp_path) -> None:
    path = tmp_path / "broken.jsonl"
    path.write_text("".join(json.dumps({"id": i}) + "\n" if i not in (3, 7) else '{"id": %d, "bad": }\n' % i
                            for i in range(12)), encoding="utf-8")
    s = opened(path)
    assert s.doc.broken_count == 2
    out = tmp_path / "out"
    out.mkdir()
    dlg = split_dialog(s)
    dlg.set_out_dir(out)
    dlg.set_parts(2, "records")
    assert wait_plan(qtbot, dlg) is None
    assert t("gui.filetools.hint.broken") in dlg.error_text() and not dlg.run_button.isEnabled()
    assert dlg.drop_broken_check.isEnabled()
    dlg.drop_broken_check.setChecked(True)
    outcome = run_split(qtbot, dlg)
    assert "filetools.warn.broken_dropped" in outcome.report.warning_keys
    ids = [json.loads(line)["id"] for p in outcome.report.files for line in p.read_text(encoding="utf-8").splitlines()]
    assert ids == [i for i in range(12) if i not in (3, 7)]


# ----------------------------------------------------------------------------------------
# split: refusals, progress / stop, existing parts, the report
# ----------------------------------------------------------------------------------------
def test_split_refusals(qtbot, app, opened, split_dialog, tmp_path) -> None:
    src = write_array(tmp_path / "data_1.json", 30)
    s = opened(src)
    dlg = split_dialog(s)
    for text in ("abc", "0", "-5 MB", ""):
        dlg.set_size_text(text)
        assert wait_plan(qtbot, dlg) is None
        assert UserInputError("gui.filetools.error.bad_size", text=text.strip()).message in dlg.error_text()
        assert not dlg.run_button.isEnabled()
    dlg.set_size_text("1 GB")
    assert wait_plan(qtbot, dlg) is not None and dlg.error_text() == ""
    dlg.set_parts(50)
    assert wait_plan(qtbot, dlg) is None
    assert UserInputError("filetools.error.too_many_parts", parts=50, count=30).message in dlg.error_text()
    dlg.set_parts(2)
    missing = tmp_path / "yo'q papka"
    dlg.set_out_dir(missing)
    assert wait_plan(qtbot, dlg) is None
    assert UserInputError("filetools.error.no_dir", path=str(missing)).message in dlg.error_text()
    a_file = tmp_path / "fayl.txt"
    a_file.write_text("x", encoding="utf-8")
    dlg.set_out_dir(a_file)
    assert wait_plan(qtbot, dlg) is None
    assert t("gui.filetools.error.not_dir", path=str(a_file)) in dlg.error_text()
    dlg.set_out_dir("")
    assert wait_plan(qtbot, dlg) is None and t("gui.filetools.error.need_folder") in dlg.error_text()
    # a part that would be the source file itself
    dlg.set_out_dir(tmp_path)
    dlg.set_name_pattern("data_{n}.json")
    assert wait_plan(qtbot, dlg) is None
    assert UserInputError("writer.error.same_file", path=str(src)).message in dlg.error_text()
    # a read-only part
    dlg.set_name_pattern("part_{n}{ext}")
    locked = tmp_path / "part_2.json"
    locked.write_text("[]", encoding="utf-8")
    os.chmod(locked, stat.S_IREAD)
    try:
        assert wait_plan(qtbot, dlg) is None
        assert UserInputError("fileio.error.read_only", path=str(locked)).message in dlg.error_text()
    finally:
        os.chmod(locked, stat.S_IREAD | stat.S_IWRITE)
    dlg.set_out_dir(str(tmp_path) + os.sep + ".")      # "." is resolved
    assert dlg.out_dir() == str(tmp_path)
    plan = wait_plan(qtbot, dlg)
    assert plan is not None and plan.existing == [locked]
    assert t("gui.filetools.split.plan.existing", count=1, names="part_2.json") in dlg.warning_text()


def test_progress_stop_and_existing_parts(qtbot, app, opened, split_dialog, tmp_path, calls, monkeypatch) -> None:
    src = write_array(tmp_path / "people.json", 20)
    s = opened(src)
    out = tmp_path / "out"
    out.mkdir()
    dlg = split_dialog(s)
    dlg.set_out_dir(out)
    dlg.set_parts(2, "records")
    first = run_split(qtbot, dlg)
    old = {p: p.read_bytes() for p in first.report.files}
    plan = wait_plan(qtbot, dlg)
    assert plan.existing == first.report.files and not calls.questions
    # replacing existing parts asks first; "no" starts nothing
    calls.answer = False
    assert dlg.start_split() is None and len(calls.questions) == 1
    assert "people_1.json" in calls.questions[0]
    # progress and stop: nothing is written
    calls.answer = True
    real = filetools.split
    started = threading.Event()

    def slow_split(doc: Any, opts: Any, source: Any = None, *, progress: Any = None, cancel: Any = None) -> Any:
        progress.set_total(1000)
        progress.update(400)
        progress.set_message("sekin bo'lish")
        started.set()
        while not cancel.cancelled:
            time.sleep(0.005)
        return real(doc, opts, source, progress=progress, cancel=cancel)   # raises Cancelled at once

    monkeypatch.setattr(filetools, "split", slow_split)
    dlg.set_parts(4, "records")
    wait_plan(qtbot, dlg)
    task = dlg.start_split()
    assert task is not None and dlg.running and not dlg.run_button.isEnabled()
    qtbot.waitUntil(started.is_set, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: dlg.progress_bar.maximum() == 1000 and dlg.progress_bar.value() == 400, timeout=TIMEOUT)
    assert not dlg.stop_button.isHidden() and dlg.status_label.text() == "sekin bo'lish"
    assert task in app.tasks.tasks() and task.kind == "split"
    with qtbot.waitSignal(dlg.runner.job_finished, timeout=TIMEOUT):
        dlg.stop_button.click()
    assert task.state == "cancelled" and not dlg.running
    assert dlg.status_label.text() == t("gui.filetools.status.cancelled")
    assert dlg.stop_button.isHidden() and dlg.progress_bar.isHidden()
    assert sorted(os.listdir(out)) == ["people_1.json", "people_2.json"]
    assert {p: p.read_bytes() for p in first.report.files} == old and temps(out) == []
    assert not app.errors


def test_report_window_and_callbacks(qtbot, app, opened, split_dialog, tmp_path, calls) -> None:
    s = opened(write_array(tmp_path / "people.json", 9))
    out = tmp_path / "out"
    out.mkdir()
    dlg = split_dialog(s)
    dlg.set_out_dir(out)
    dlg.set_records(3)
    outcome = run_split(qtbot, dlg)
    rep = dlg.report_dialog
    assert isinstance(rep, ftd.FileToolsReportDialog) and rep.isVisible()
    assert rep.outcome is outcome and rep.files() == outcome.report.files
    text = rep.report_text()
    for p in outcome.report.files:
        assert str(p) in text and ftd.size_text(p.stat().st_size) in text
    assert t("gui.filetools.report.split", parts=3, records=9, size=ftd.size_text(outcome.report.bytes_written),
             seconds="", rate="").split(":")[0] in rep.summary_text()
    rep.open_folder_button.click()
    rep.open_result_button.click()
    assert calls.folders == [str(out)] and calls.files == [str(outcome.report.files[0])]
    assert any(str(out) in text for text in app.statuses)
    # the default "open the result" asks the application to open a tab (never the system handler)
    dlg2 = ftd.FileToolsReportDialog(outcome, None, open_folder=calls.open_folder,
                                     open_file=ftd._default_open_file(app))
    qtbot.addWidget(dlg2)
    dlg2.open_result()
    assert app.requested == [str(outcome.report.files[0])]


def test_gui_thread_reads_no_data(qtbot, app, opened, split_dialog, tmp_path, monkeypatch) -> None:
    probe = GuiReads(monkeypatch)
    s = opened(write_array(tmp_path / "people.json", 300))
    out = tmp_path / "out"
    out.mkdir()
    dlg = split_dialog(s)
    dlg.set_out_dir(out)
    dlg.set_size_text("8 KB")
    run_split(qtbot, dlg)
    assert probe.big() == []


def test_dialog_waits_for_indexing_remembers_options_and_follows_the_tab(qtbot, app, split_dialog, tmp_path,
                                                                          calls) -> None:
    src = write_array(tmp_path / "people.json", 12)
    s = DocSession(app, src, use_cache=False)
    s.start()
    dlg = split_dialog(s)                       # the file is still being opened
    assert dlg.plan is None and not dlg.run_button.isEnabled()
    qtbot.waitUntil(lambda: s.state == "ready", timeout=TIMEOUT)
    plan = wait_plan(qtbot, dlg)
    assert plan is not None and plan.records == 12
    # the folder chooser (injected) and the options remembered for the next dialog
    other = tmp_path / "boshqa papka"
    other.mkdir()
    calls.folder_answer = str(other)
    assert dlg.choose_folder() == str(other) and dlg.out_dir() == str(other)
    dlg.set_records(5)
    dlg.set_format("jsonl")
    ftd._select_data(dlg.indent_combo, 4)
    run_split(qtbot, dlg)
    assert sorted(os.listdir(other)) == ["people_1.jsonl", "people_2.jsonl", "people_3.jsonl"]
    again = split_dialog(s)
    assert again.mode() == "records" and again.records_spin.value() == 5 and again.format() == "jsonl"
    assert again.indent_combo.currentData() == 4 and again.out_dir() == str(tmp_path)   # (the folder is not kept)
    # the tab is closed: nothing can be planned or started any more
    s.close()
    qtbot.waitUntil(lambda: t("gui.filetools.error.closed") in dlg.error_text(), timeout=TIMEOUT)
    assert not dlg.run_button.isEnabled() and dlg.start_split() is None
    assert app.tasks.wait_idle(10)


def test_show_split_dialog_and_closing(qtbot, app, opened, tmp_path, calls) -> None:
    s = opened(write_wrapped(tmp_path / "w.json", 6))
    dlg = ftd.show_split_dialog(None, s, open_folder=calls.open_folder, open_file=calls.open_file)
    assert dlg.isVisible() and not dlg.isModal() and dlg.source() == "document"
    again = ftd.show_split_dialog(None, s, source=NodeRef(None, ("data",)))
    assert again is dlg and dlg.source() == "node"
    assert ftd.SplitDialog.instance(s) is dlg
    wait_plan(qtbot, dlg)
    dlg.set_parts(3)
    task = dlg.plan_now()
    dlg.close()                                   # the plan task is cancelled and forgotten
    assert ftd.SplitDialog.instance(s) is None
    assert task is None or task.cancel_requested or task.state != "running"
    assert app.tasks.wait_idle(10)


# ----------------------------------------------------------------------------------------
# merge
# ----------------------------------------------------------------------------------------
def test_merge_files_and_an_edited_tab(qtbot, app, opened, merge_dialog, tmp_path, backend) -> None:
    a = write_array(tmp_path / "a.json", 5, 0, "A")
    b = write_jsonl(tmp_path / "b.jsonl", 4, 100, "B")
    c = write_array(tmp_path / "c.json", 3, 200, "C")
    sc = opened(c, edit=True)
    run(qtbot, sc.edit.set_value, NodeRef(1, ("name",)), '"Tahrirlangan Ўзбек"')
    dlg = merge_dialog(lambda: [sc])
    dlg.add_files([a, b])
    dlg.add_session(sc)
    wait_inputs(qtbot, dlg)
    texts = dlg.input_texts()
    assert texts[0].startswith("1. a.json — ") and "5" in texts[0]
    assert texts[1].startswith("2. b.jsonl — ") and "4" in texts[1]
    assert texts[2] == "3. " + t("gui.filetools.merge.input.tab_dirty", name="c.json", kind=t("reader.kind.array"),
                                 records=3)
    assert dlg.note_text() == t("gui.filetools.merge.edited_note", names="c.json")
    assert Path(dlg.output_path()) == tmp_path / "a_merged.json"
    outcome = run_merge(qtbot, dlg)
    out = tmp_path / "a_merged.json"
    report = outcome.report
    assert report.records == 12 and [w for _p, _k, _n, w in report.inputs] == [5, 4, 3]
    assert outcome.edited == ["c.json"] and outcome.size == out.stat().st_size
    got = [compact(r) for r in records_of(out)]
    pinned = sc.doc.pinned()
    want = records_of(a) + records_of(b) + [compact(pinned.record_bytes(i)) for i in range(3)]
    assert got == want and b"Tahrirlangan" in got[-2]
    # the core with the same options and the same snapshot writes the same bytes
    da, db = open_doc(a), open_doc(b)
    try:
        filetools.merge([da, db, pinned], tmp_path / "ref.json", dlg.merge_options())
    finally:
        da.close()
        db.close()
    assert out.read_bytes() == (tmp_path / "ref.json").read_bytes()
    text = out.read_text(encoding="utf-8")
    assert "1e400" in text and "12345678901234567890" in text and "-0.0" in text and "Самарқанд" in text
    rep = dlg.report_dialog
    assert rep is not None and "a.json" in rep.inputs_text() and str(out) in rep.files_text()
    assert not app.errors


def test_inputs_add_remove_reorder(qtbot, app, opened, merge_dialog, tmp_path, calls) -> None:
    files = [write_array(tmp_path / f"f{i}.json", 2, 10 * i) for i in range(3)]
    s = opened(write_array(tmp_path / "tab.json", 2, 90))
    dlg = merge_dialog(lambda: [s])
    calls.files_answer = [str(p) for p in files]
    dlg.choose_files()
    assert [Path(i.path).name for i in dlg.inputs()] == ["f0.json", "f1.json", "f2.json"]
    dlg._fill_tabs_menu()
    actions = [a for a in dlg.tabs_menu.actions() if a.text()]
    assert actions[0].text() == "tab.json" and actions[-1].text() == t("gui.filetools.merge.add_tabs.all")
    actions[0].trigger()
    assert dlg.inputs()[-1].is_tab and dlg.add_open_tabs() == 0      # (listed already)
    assert dlg.move_input(3, -2) == 1
    assert [i.name for i in dlg.inputs()] == ["f0.json", "tab.json", "f1.json", "f2.json"]
    dlg.list_widget.setFocus()
    dlg.list_widget.setCurrentRow(3)
    qtbot.keyClick(dlg.list_widget, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
    assert [i.name for i in dlg.inputs()] == ["f0.json", "tab.json", "f2.json", "f1.json"]
    assert dlg.list_widget.currentRow() == 2
    qtbot.keyClick(dlg.list_widget, Qt.Key.Key_Delete)
    assert [i.name for i in dlg.inputs()] == ["f0.json", "tab.json", "f1.json"]
    dlg.list_widget.setCurrentRow(0)
    assert not dlg.up_button.isEnabled() and dlg.down_button.isEnabled()
    dlg.remove_selected()
    assert [i.name for i in dlg.inputs()] == ["tab.json", "f1.json"]
    outcome = run_merge(qtbot, dlg)
    assert [json.loads(compact(r))["id"] for r in records_of(Path(outcome.report.report.files[0]))] == [90, 91, 10, 11]
    dlg.remove_input(0)
    dlg.remove_input(0)
    assert not dlg._validate() and t("gui.filetools.merge.error.no_inputs") in dlg.error_text()


def test_merge_output_chooser_and_dropped_files(qtbot, app, merge_dialog, tmp_path, calls) -> None:
    from PySide6.QtCore import QMimeData, QPointF, QUrl
    from PySide6.QtGui import QDropEvent

    a = write_array(tmp_path / "a.json", 2)
    b = write_jsonl(tmp_path / "b.jsonl", 2, 7)
    dlg = merge_dialog()
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(a)), QUrl.fromLocalFile(str(b)), QUrl.fromLocalFile(str(tmp_path))])
    event = QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime, Qt.MouseButton.NoButton,
                       Qt.KeyboardModifier.NoModifier)
    dlg.dropEvent(event)
    assert [i.name for i in dlg.inputs()] == ["a.json", "b.jsonl"]          # (the folder is ignored)
    calls.output_answer = str(tmp_path / "natija")
    assert dlg.choose_output() == str(tmp_path / "natija.json")
    assert dlg.output_path() == str(tmp_path / "natija.json") and dlg.format() == "json"
    calls.output_answer = str(tmp_path / "natija.jsonl.gz")
    dlg.choose_output()
    assert dlg.format() == "jsonl"
    outcome = run_merge(qtbot, dlg)
    lines = gzip.decompress((tmp_path / "natija.jsonl.gz").read_bytes()).splitlines()
    assert [json.loads(x)["id"] for x in lines] == [0, 1, 7, 8]
    assert outcome.size == (tmp_path / "natija.jsonl.gz").stat().st_size


def test_merge_jsonl_indent_sort_keys_and_format_follows_the_name(qtbot, app, merge_dialog, tmp_path) -> None:
    a = write_array(tmp_path / "a.json", 3)
    b = write_array(tmp_path / "b.json", 2, 3)
    dlg = merge_dialog()
    dlg.add_files([a, b])
    dlg.set_format("jsonl")
    assert dlg.output_path().endswith("a_merged.jsonl")
    assert not dlg.indent_combo.isEnabled() and not dlg.keep_wrapper_check.isEnabled()
    outcome = run_merge(qtbot, dlg)
    lines = Path(outcome.report.report.files[0]).read_bytes().splitlines()
    assert [json.loads(x)["id"] for x in lines] == [0, 1, 2, 3, 4] and b"1e400" in lines[0]
    # a JSON name switches the format; tab indentation, keys sorted alphabetically
    dlg.set_output_path(tmp_path / "sorted.json")
    assert dlg.format() == "json" and dlg.indent_combo.isEnabled()
    ftd._select_data(dlg.indent_combo, "\t")
    ftd._select_data(dlg.sort_combo, "alpha")
    outcome = run_merge(qtbot, dlg)
    text = (tmp_path / "sorted.json").read_text(encoding="utf-8")
    assert text.startswith('[\n\t{\n\t\t"city": ')
    keys = list(json.loads(text)[0])
    assert keys == ["city", "id", "name", "nested", "nums", "src"]
    da, db = open_doc(a), open_doc(b)
    try:
        filetools.merge([da, db], tmp_path / "ref.json", dlg.merge_options())
    finally:
        da.close()
        db.close()
    assert text.encode("utf-8") == (tmp_path / "ref.json").read_bytes()
    # a CSV name is refused (merge writes JSON / JSON Lines)
    dlg.set_output_path(tmp_path / "x.csv")
    assert not dlg._validate() and t("gui.filetools.hint.merge_format") in dlg.error_text()


def test_merge_keeps_the_first_wrapper_or_not(qtbot, app, merge_dialog, tmp_path) -> None:
    w = write_wrapped(tmp_path / "w.json", 3)
    a = write_array(tmp_path / "a.json", 2, 50)
    dlg = merge_dialog()
    dlg.add_files([w, a])
    assert dlg.keep_wrapper_check.isChecked()
    outcome = run_merge(qtbot, dlg)
    value = json.loads(Path(outcome.report.report.files[0]).read_text(encoding="utf-8"))
    assert value["meta"]["nom"] == "Ўзбек" and [r["id"] for r in value["data"]] == [0, 1, 2, 50, 51]
    dlg.keep_wrapper_check.setChecked(False)
    dlg.set_output_path(tmp_path / "bare.json")
    run_merge(qtbot, dlg)
    value = json.loads((tmp_path / "bare.json").read_text(encoding="utf-8"))
    assert [r["id"] for r in value] == [0, 1, 2, 50, 51]


def test_merge_dedupe_keep_first_or_last(qtbot, app, merge_dialog, tmp_path, backend) -> None:
    a = write_array(tmp_path / "a.json", 10, 0, "A")
    b = write_jsonl(tmp_path / "b.jsonl", 10, 5, "B")
    dlg = merge_dialog()
    dlg.add_files([a, b])
    wait_inputs(qtbot, dlg)
    assert {"id", "src", "nested.x"} <= set(dlg.key_suggestions())
    assert dlg.key_suggestions()[:2] == ["$", "$key"]
    dlg.set_dedupe("id", "first")
    outcome = run_merge(qtbot, dlg)
    rows = [json.loads(r) for r in records_of(tmp_path / "a_merged.json")]
    assert [(r["id"], r["src"]) for r in rows] == [(i, "A") for i in range(10)] + [(i, "B") for i in range(10, 15)]
    assert outcome.report.duplicates == 5 and outcome.report.groups == 5
    assert t("gui.filetools.report.dupes", duplicates=5, groups=5) in dlg.report_dialog.summary_text()
    dlg.set_dedupe("id", "last")
    run_merge(qtbot, dlg)
    rows = [json.loads(r) for r in records_of(tmp_path / "a_merged.json")]
    assert [(r["id"], r["src"]) for r in rows] == [(i, "A") for i in range(5)] + [(i, "B") for i in range(5, 15)]
    # the whole record: nothing is equal here
    dlg.set_dedupe("$")
    assert run_merge(qtbot, dlg).report.duplicates == 0
    # keys are checked at once
    dlg.set_dedupe("nums[*]")
    assert not dlg._validate()
    assert UserInputError("dupes.error.wildcard", key="nums[*]").message in dlg.error_text()
    dlg.set_dedupe("")
    assert not dlg._validate() and t("gui.filetools.merge.error.need_key") in dlg.error_text()
    dlg.set_dedupe(None)
    assert dlg._validate() and dlg.merge_options().dedupe_by is None


def test_merge_output_is_never_an_input(qtbot, app, opened, merge_dialog, tmp_path) -> None:
    a = write_array(tmp_path / "a.json", 3)
    before = a.read_bytes()
    s = opened(write_array(tmp_path / "tab.json", 2, 10))
    dlg = merge_dialog(lambda: [s])
    dlg.add_files([a])
    dlg.add_session(s)
    for target in (a, tmp_path / "tab.json"):
        dlg.set_output_path(target)
        assert not dlg._validate() and not dlg.run_button.isEnabled()
        assert t("gui.filetools.merge.error.output_is_input", path=str(target)) in dlg.error_text()
        assert dlg.start_merge() is None
    if os.name == "nt":                            # another spelling of the same file
        dlg.set_output_path(str(a).upper())
        assert not dlg._validate()
    dlg.set_output_path(tmp_path / "ok.json")
    assert dlg._validate()
    assert a.read_bytes() == before                # untouched


def test_merge_partial_inputs_and_a_closed_tab(qtbot, app, opened, merge_dialog, tmp_path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('[{"id": 1}, {"id": 2}, {"id": ]', encoding="utf-8")
    s = opened(write_array(tmp_path / "tab.json", 2, 10))
    dlg = merge_dialog(lambda: [s] if not s.closed else [])
    dlg.add_files([bad])
    dlg.add_session(s)
    wait_inputs(qtbot, dlg)
    assert t("gui.filetools.hint.partial_input") in dlg.error_text() and not dlg.run_button.isEnabled()
    assert dlg.input_texts()[0].startswith("1. " + t("gui.filetools.merge.input.partial", name="bad.json", line=1,
                                                     records=2))
    dlg.allow_partial_check.setChecked(True)
    # the tab is closed: its file is read from disk
    s.close()
    qtbot.waitUntil(lambda: not dlg.inputs()[1].is_tab and dlg.inputs()[1].info is not None, timeout=TIMEOUT)
    assert dlg.input_texts()[1].startswith("2. tab.json — ")
    outcome = run_merge(qtbot, dlg)
    rows = [json.loads(r) for r in records_of(Path(outcome.report.report.files[0]))]
    assert [r["id"] for r in rows] == [1, 2, 10, 11]
    assert "writer.warn.partial" in outcome.report.warning_keys


def test_merge_progress_and_stop(qtbot, app, merge_dialog, tmp_path, monkeypatch) -> None:
    a = write_array(tmp_path / "a.json", 3)
    out = tmp_path / "out.json"
    out.write_text("[]", encoding="utf-8")
    real = filetools.merge
    started = threading.Event()

    def slow_merge(docs: Any, out_path: Any, opts: Any, *, progress: Any = None, cancel: Any = None) -> Any:
        progress.set_total(200)
        progress.update(50)
        progress.set_message("sekin birlashtirish")
        started.set()
        while not cancel.cancelled:
            time.sleep(0.005)
        return real(docs, out_path, opts, progress=progress, cancel=cancel)

    monkeypatch.setattr(filetools, "merge", slow_merge)
    calls_seen: list[str] = []
    monkeypatch.setattr(widgets, "confirm_handler", lambda _p, text, _t: calls_seen.append(text) or True)
    dlg = merge_dialog()
    dlg.add_files([a])
    dlg.set_output_path(out)
    wait_inputs(qtbot, dlg)
    task = dlg.start_merge()
    assert task is not None and calls_seen == [t("gui.filetools.merge.overwrite", path=str(out))]
    qtbot.waitUntil(started.is_set, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: dlg.progress_bar.value() == 250, timeout=TIMEOUT)
    assert dlg.status_label.text() == "sekin birlashtirish" and not dlg.run_button.isEnabled()
    with qtbot.waitSignal(dlg.runner.job_finished, timeout=TIMEOUT):
        dlg.cancel_task()
    assert task.state == "cancelled" and dlg.status_label.text() == t("gui.filetools.status.cancelled")
    assert out.read_text(encoding="utf-8") == "[]" and temps(tmp_path) == []
    assert dlg.run_button.isEnabled()


def test_show_merge_dialog_one_per_application(qtbot, app, opened, tmp_path, calls) -> None:
    s = opened(write_array(tmp_path / "tab.json", 2))
    other = write_array(tmp_path / "other.json", 2, 5)
    dlg = ftd.show_merge_dialog(None, app, lambda: [s], inputs=[s], open_folder=calls.open_folder,
                                open_file=calls.open_file)
    assert dlg.isVisible() and not dlg.isModal()
    again = ftd.show_merge_dialog(None, app, inputs=[s, other])
    assert again is dlg and [i.name for i in dlg.inputs()] == ["tab.json", "other.json"]
    assert dlg.inputs()[0].is_tab and not dlg.inputs()[1].is_tab
    assert ftd.MergeDialog.instance(app) is dlg
    dlg.close()
    assert ftd.MergeDialog.instance(app) is None
    assert app.tasks.wait_idle(10)


# ----------------------------------------------------------------------------------------
# texts, helpers
# ----------------------------------------------------------------------------------------
def test_uzbek_and_english_texts(qtbot, app, opened, split_dialog, merge_dialog, tmp_path, lang_uz) -> None:
    s = opened(write_array(tmp_path / "people.json", 30))
    dlg = split_dialog(s)
    assert dlg.windowTitle() == "Faylni bo'lish — people.json"
    assert dlg.run_button.text() == "&Bo'lish" and dlg.close_button.text() == "Yopish"
    assert dlg.source_buttons["document"].text() == "Butun fayl (30 ta yozuv)"
    assert dlg.mode_buttons["parts"].text() == "Qismlar soni:"
    assert dlg.keep_wrapper_check.text() == "Har bir qismda o'ram (wrapper) saqlansin"
    dlg.set_size_text("abc")
    wait_plan(qtbot, dlg)
    assert "Qism hajmi noto'g'ri: «abc»" in dlg.error_text()
    merge = merge_dialog()
    assert merge.windowTitle() == "Fayllarni birlashtirish" and merge.run_button.text() == "&Birlashtirish"
    assert merge.dedupe_box.title() == "Takroriy yozuvlarni olib tashlash"
    assert t("gui.filetools.action.split") == "Faylni bo'lish…"
    assert t("gui.filetools.action.merge") == "Fayllarni birlashtirish…"
    from jsonreshape import i18n

    i18n.set_language("en")
    assert t("gui.filetools.split.title", name="x") == "Split file — x"
    assert t("gui.filetools.action.merge") == "Merge files…"


def test_sizes_and_names_helpers(tmp_path) -> None:
    assert ftd.parse_part_size("100") == 100 << 20
    assert ftd.parse_part_size("1,5 GB") == ftd.parse_part_size("1.5gb") == 3 << 29
    assert ftd.parse_part_size("2 GiB") == 2 << 30 and ftd.parse_part_size("64 KB") == 64 << 10
    assert ftd.parse_part_size("700 B") == 700
    for bad in ("", "abc", "0", "-1", "1e400", "nan", "1.5.5 MB"):
        with pytest.raises(UserInputError):
            ftd.parse_part_size(bad)
    assert ftd.size_text(100 << 20) == "100.0 MB" and ftd.size_text(1023) == "1023 B"
    assert ftd.size_text(1536) == "1.5 KB" and ftd.size_text((1 << 30) - 1) == "1.0 GB"
    assert ftd._stem_ext("x.jsonl.gz") == ("x", ".jsonl.gz") and ftd._stem_ext("a.b.json") == ("a.b", ".json")
    err = UserInputError("filetools.error.broken_lines", count=3)
    assert ftd.error_text(err) == err.message + "\n" + t("gui.filetools.hint.broken")
    assert "--" not in ftd.error_text(UserInputError("filetools.error.single"))
    assert ftd.error_text(ValueError("boom")) == t("gui.filetools.error.unexpected", error="boom")
