"""SearchPanel (jsonreshape/gui/searchpanel.py; ARCHITECTURE §11.8, AGENT_PROMPT §6.2).

Live results on a generated 50 000-record file (hits listed while the task still runs),
results identical to ``search.reference_search`` for plain / apostrophe variants /
Latin<->Cyrillic / regex / number / keys only / path filter, capped lists whose count goes
on, activating a hit (current node + reveal), opening the hits as a record set, CSV/JSON
export of every hit, the persisted history restoring options, cancelling (button, Esc,
a new search, a closed tab), per-session results, a filtered view as the source, inline
input errors, the context menu.
"""

from __future__ import annotations

import csv
import io
import json
import threading
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication

from jsonreshape import search as search_mod
from jsonreshape.errors import UserInputError
from jsonreshape.gui.context import AppContext
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.searchpanel import SearchPanel, hit_ref, snapshot_recordset
from jsonreshape.gui.session import DocSession, FilterSpec, NodeRef
from jsonreshape.recordset import RecordSet
from jsonreshape.search import SearchHistory, SearchOptions

pytestmark = pytest.mark.gui

TIMEOUT = 30000
N = 50_000
CITIES = ["Toshkent", "Тошкент", "O'zbekiston", "Oʻzbekiston", "Samarqand", "Самарқанд", "Buxoro",
          "G'ijduvon", "Farg‘ona", "toshkent shahri", "Ўзбекистон"]


# ----------------------------------------------------------------------------------------
# data and fixtures
# ----------------------------------------------------------------------------------------
def make_record(i: int) -> dict:
    return {"id": i, "name": f"user{i}", "city": CITIES[i % len(CITIES)], "grp": i % 3,
            "amount": i * 7 % 100003,
            "contact": {"city": CITIES[(i * 3) % len(CITIES)], "phone": f"+998{i:09d}"},
            "tags": [CITIES[(i + 1) % len(CITIES)], "x"]}


def write_records(path: Path, n: int) -> Path:
    path.write_bytes(("[" + ",\n".join(json.dumps(make_record(i), ensure_ascii=False) for i in range(n)) + "]")
                     .encode("utf-8"))
    return path


@pytest.fixture(scope="module")
def big_path(tmp_path_factory) -> Path:
    return write_records(tmp_path_factory.mktemp("search") / "people.json", N)


@pytest.fixture
def small_path(tmp_path: Path) -> Path:
    return write_records(tmp_path / "small.json", 300)


@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(10)
    ctx.errors = []
    ctx.error_reported.connect(ctx.errors.append)
    yield ctx
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
        assert s.state == "ready"
        return s

    yield factory
    for s in made:
        s.close()
    assert app.tasks.wait_idle(30)


@pytest.fixture
def panel(qtbot, app):
    p = SearchPanel(app)
    qtbot.addWidget(p)
    assert app.tasks.wait_idle(10)     # history loaded
    yield p
    p.shutdown()


def run(qtbot, panel: SearchPanel, text: str, **options) -> None:
    """Run a search with ``options`` (SearchOptions.to_dict keys) and wait for its end."""
    panel.set_options(dict(options, text=text))
    with qtbot.waitSignal(panel.search_finished, timeout=TIMEOUT) as blocker:
        assert panel.run_search() is not None, panel.error_text()
    assert blocker.args[1] == "done", panel.summary_text()


class Gate:
    """Wrap ``search.search``: after the first batch of hits the worker waits for the gate."""

    def __init__(self, monkeypatch) -> None:
        self.event = threading.Event()
        self.batches = 0
        real = search_mod.search

        def gated(doc, opts, source=None, *, on_hits=None, **kw):
            def hook(batch):
                on_hits(batch)
                self.batches += 1
                if self.batches == 1:
                    self.event.wait(20)

            return real(doc, opts, source, on_hits=hook if on_hits else None, **kw)

        monkeypatch.setattr(search_mod, "search", gated)

    def open(self) -> None:
        self.event.set()


# ----------------------------------------------------------------------------------------
# live results, exact results
# ----------------------------------------------------------------------------------------
def test_hits_stream_in_before_the_task_ends(qtbot, app, opened, panel, big_path, monkeypatch, lang_uz) -> None:
    gate = Gate(monkeypatch)
    s = opened(big_path)
    panel.set_session(s)
    panel.set_options({"text": "toshkent", "mode": "plain"})
    task = panel.run_search()
    assert task is not None and task.kind == "search" and task.owner is s
    try:
        qtbot.waitUntil(lambda: panel.hit_count_shown() > 0, timeout=TIMEOUT)
        # the first hits are listed while the search still runs
        assert task.state == "running" and task.thread_alive
        assert panel.is_running() and panel.cancel_button.isEnabled()
        assert "Qidirilmoqda" in panel.summary_text()
        assert panel.progress_bar.isVisible() or not panel.isVisible()
        assert not panel.open_button.isEnabled() and not panel.export_button.isEnabled()
        assert panel.export_hits_to(str(big_path.with_name("early.csv"))) is None
    finally:
        gate.open()
    with qtbot.waitSignal(panel.search_finished, timeout=TIMEOUT):
        pass
    st = panel.state()
    ref = search_mod.reference_search(s.doc, panel.options())
    assert st.status == "done" and st.hits == ref
    assert st.hit_count == len(ref) == panel.hit_count_shown()
    assert st.record_count == len({h.record for h in ref})
    assert panel.summary_text() == st.result.summary()
    assert not panel.cancel_button.isEnabled() and panel.open_button.isEnabled()


CASES = [
    ("plain", {"text": "toshkent"}),
    ("apostrophes", {"text": "o‘zbekiston"}),
    ("translit", {"text": "Toshkent", "translit": True}),
    ("translit_cyrillic", {"text": "ўзбекистон", "translit": True}),
    ("case", {"text": "Toshkent", "case_sensitive": True}),
    ("whole_word", {"text": "toshkent", "whole_word": True}),
    ("regex", {"text": r"^user4\d{3}$", "mode": "regex"}),
    ("number", {"text": "> 99990", "mode": "number"}),
    ("keys_only", {"text": "phone", "scope": "keys"}),
    ("values_only", {"text": "city", "scope": "values"}),
    ("path", {"text": "toshkent", "path": "contact.city"}),
    ("path_any_depth", {"text": "samarqand", "path": "**.city", "translit": True}),
]


def test_results_equal_reference_search(qtbot, app, opened, panel, big_path, lang_uz) -> None:
    s = opened(big_path)
    panel.set_session(s)
    defaults = {"mode": "plain", "case_sensitive": False, "whole_word": False, "translit": False, "scope": "both",
                "path": ""}
    for name, options in CASES:
        run(qtbot, panel, **dict(defaults, **options))
        st = panel.state()
        opts = SearchOptions.from_dict(dict(defaults, **options))
        ref = search_mod.reference_search(s.doc, opts)
        assert st.opts == opts or st.opts.to_dict() == opts.to_dict(), name
        assert st.hits == ref, name
        assert st.hit_count == len(ref) and not st.truncated, name
        assert st.record_count == len({h.record for h in ref}), name
        assert panel.hit_count_shown() == len(ref), name
    # sanity: the cases really differ
    assert len(CASES) == len({name for name, _ in CASES})


def test_cells_and_snippets(qtbot, app, opened, panel, small_path, lang_uz) -> None:
    s = opened(small_path)
    panel.set_session(s)
    run(qtbot, panel, "tosh", translit=True)
    cols = {c.id: c for c in panel.grid.columns()}
    assert list(cols) == ["record", "path", "where", "text"]
    src = panel.grid.source()
    first = panel.hit_at(0)
    assert src.cell(0, cols["record"]).text == f"#{first.record}"
    assert src.cell(0, cols["path"]).text == first.path_text
    assert src.cell(0, cols["where"]).text == "qiymat"
    text_cell = src.cell(0, cols["text"])
    assert text_cell.text == first.text and text_cell.spans == first.spans and text_cell.spans
    for row in range(panel.hit_count_shown()):
        cell = src.cell(row, cols["text"])
        for a, b in cell.spans:
            assert "tosh" in cell.text[a:b].lower() or "тош" in cell.text[a:b].lower()


def test_capped_list_keeps_counting_and_exports_everything(qtbot, app, opened, panel, big_path, tmp_path,
                                                          lang_uz) -> None:
    s = opened(big_path)
    panel.set_session(s)
    panel.max_hits = 150
    run(qtbot, panel, "phone", scope="keys")
    st = panel.state()
    assert panel.hit_count_shown() == 150 and st.truncated
    assert st.hit_count == N and st.record_count == N
    assert "150" in panel.summary_text() and "50 000" in panel.summary_text()
    out = tmp_path / "hits.csv"
    with qtbot.waitSignal(panel.export_finished, timeout=TIMEOUT) as blocker:
        assert panel.export_hits_to(str(out)) is not None
    assert blocker.args == [str(out), N]
    rows = list(csv.reader(io.StringIO(out.read_text(encoding="utf-8-sig"))))
    assert rows[0] == ["record", "key", "path", "where", "text"]
    assert len(rows) == N + 1
    assert rows[1] == ["0", "", "contact.phone", "key", "phone"]
    assert rows[-1][0] == str(N - 1)
    assert "saqlandi" in panel.summary_text()


# ----------------------------------------------------------------------------------------
# navigation, record sets, export, history
# ----------------------------------------------------------------------------------------
def test_activating_a_hit_selects_and_reveals_it(qtbot, app, opened, panel, small_path) -> None:
    s = opened(small_path)
    panel.set_session(s)
    run(qtbot, panel, "Buxoro", path="contact.city")
    hit = panel.hit_at(2)
    with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT) as blocker:
        panel.grid.activated.emit(2, 3)
    assert blocker.args[0] == NodeRef(hit.record, ("contact", "city"))
    assert s.current == NodeRef(hit.record, ("contact", "city"))
    # Enter on the grid activates the current row
    panel.grid.set_current(0)
    with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT) as blocker:
        qtbot.keyClick(panel.grid, Qt.Key.Key_Return)
    assert blocker.args[0].record == panel.hit_at(0).record


def test_single_document_hit_refs_are_absolute(qtbot, app, opened, panel, tmp_path) -> None:
    path = tmp_path / "single.json"
    path.write_text(json.dumps({"a": {"b": ["Toshkent", 1]}, "c": "toshkent"}), encoding="utf-8")
    s = opened(path, format="single")
    panel.set_session(s)
    run(qtbot, panel, "toshkent")
    assert [h.path for h in panel.state().hits] == [("a", "b", 0), ("c",)]
    assert hit_ref(s, panel.hit_at(0)) == NodeRef(None, ("a", "b", 0))
    with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT):
        panel.activate_hit(1)
    assert s.current == NodeRef(None, ("c",))


def test_keyed_document_rows_show_member_keys(qtbot, app, opened, panel, tmp_path) -> None:
    path = tmp_path / "keyed.json"
    path.write_text(json.dumps({f"id{i}": make_record(i) for i in range(30)}, ensure_ascii=False), encoding="utf-8")
    s = opened(path)
    assert s.doc.kind == "keyed"
    panel.set_session(s)
    run(qtbot, panel, "id1", scope="keys")
    hits = panel.state().hits
    assert [h.key for h in hits] == ["id1"] + [f"id{i}" for i in range(10, 20)]
    assert all(h.path == () and h.where == "key" for h in hits)
    cols = {c.id: c for c in panel.grid.columns()}
    src = panel.grid.source()
    assert src.cell(0, cols["record"]).text == "#1 · id1"
    assert src.cell(0, cols["path"]).text == "$"
    assert hits == search_mod.reference_search(s.doc, panel.state().opts)
    with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT):
        panel.activate_hit(3)
    assert s.current == NodeRef(12, ())


def test_open_hits_as_a_record_set(qtbot, app, opened, panel, big_path, lang_uz) -> None:
    s = opened(big_path)
    panel.set_session(s)
    run(qtbot, panel, "samarqand", path="city", translit=True)
    st = panel.state()
    expected = sorted({h.record for h in st.hits})
    with qtbot.waitSignal(panel.records_opened, timeout=TIMEOUT):
        assert panel.open_as_records() is not None
    assert not s.view_is_base
    assert s.view_len() == st.record_count == len(expected)
    assert [s.record_at(i) for i in range(s.view_len())] == expected
    assert "samarqand" in s.view_label() and "Qidiruv" in s.view_label()
    # the panel's own result set stays usable: open it again after resetting the view
    s.reset_view()
    with qtbot.waitSignal(panel.records_opened, timeout=TIMEOUT):
        panel.open_as_records()
    assert s.view_len() == len(expected)
    assert "joriy ko'rinish" in panel.source_label.full_text()


def test_export_hits_csv_and_json(qtbot, app, opened, panel, small_path, tmp_path) -> None:
    s = opened(small_path)
    panel.set_session(s)
    run(qtbot, panel, "o'zbekiston", path="**.city")
    st = panel.state()
    expected = [h.to_dict() for h in search_mod.reference_search(s.doc, st.opts)]
    assert expected and len(expected) == st.hit_count
    out_json = tmp_path / "hits.json"
    with qtbot.waitSignal(panel.export_finished, timeout=TIMEOUT):
        panel.export_hits_to(str(out_json))
    assert json.loads(out_json.read_text(encoding="utf-8")) == expected
    out_csv = tmp_path / "hits.CSV"
    with qtbot.waitSignal(panel.export_finished, timeout=TIMEOUT):
        panel.export_hits_to(str(out_csv))
    rows = list(csv.reader(io.StringIO(out_csv.read_text(encoding="utf-8-sig"))))
    assert [r[2] for r in rows[1:]] == [e["path"] for e in expected]
    assert [r[4] for r in rows[1:]] == [e["text"] for e in expected]
    # explicit format wins over the extension
    out_txt = tmp_path / "hits.txt"
    with qtbot.waitSignal(panel.export_finished, timeout=TIMEOUT):
        panel.export_hits_to(str(out_txt), "json")
    assert json.loads(out_txt.read_text(encoding="utf-8")) == expected


def test_export_refusals(qtbot, app, opened, panel, small_path, tmp_path, lang_uz) -> None:
    s = opened(small_path)
    panel.set_session(s)
    assert panel.export_hits_to(str(tmp_path / "x.csv")) is None       # no search yet
    assert "qidiruvni" in panel.error_text()
    run(qtbot, panel, "buxoro")
    assert panel.export_hits_to(str(small_path)) is None
    assert "manba" in panel.error_text()
    variant = Path(str(small_path).replace("small.json", "SMALL.JSON"))
    assert panel.export_hits_to(str(variant)) is None                  # case-insensitive on Windows
    assert panel.export_hits_to(str(tmp_path / "x.xml")) is None
    assert ".xml" in panel.error_text()
    assert small_path.stat().st_size > 0 and app.errors == []


def test_history_is_saved_and_restores_options(qtbot, app, opened, panel, small_path) -> None:
    s = opened(small_path)
    panel.set_session(s)
    run(qtbot, panel, r"user1\d", mode="regex", case_sensitive=True, scope="values", path="name")
    run(qtbot, panel, "buxoro", mode="plain", case_sensitive=False, scope="both", path="")
    assert app.tasks.wait_idle(10)
    entries = SearchHistory().list()
    assert [e["text"] for e in entries[:2]] == ["buxoro", r"user1\d"]
    assert entries[1]["options"]["mode"] == "regex" and entries[1]["options"]["path"] == "name"
    # a new panel (e.g. after a restart) lists them, newest first, and restores the options
    other = SearchPanel(app)
    qtbot.addWidget(other)
    with qtbot.waitSignal(other.history_changed, timeout=TIMEOUT):
        pass
    combo = other.query_combo
    assert [combo.itemText(i) for i in range(2)] == ["buxoro", r"user1\d"]
    combo.activated.emit(1)
    assert other.query_text() == r"user1\d"
    assert other.mode() == "regex" and other.case_check.isChecked() and other.scope() == "values"
    assert other.path_edit.text() == "name"
    combo.activated.emit(0)
    assert other.mode() == "plain" and not other.case_check.isChecked() and other.path_edit.text() == ""
    # running the same text again moves it to the front without a duplicate
    run(qtbot, panel, r"user1\d", mode="regex", path="name")
    assert app.tasks.wait_idle(10)
    texts = SearchHistory().texts()
    assert texts[0] == r"user1\d" and texts.count(r"user1\d") == 1
    other.shutdown()


# ----------------------------------------------------------------------------------------
# cancelling
# ----------------------------------------------------------------------------------------
def test_cancel_keeps_partial_results(qtbot, app, opened, panel, big_path, monkeypatch, lang_uz) -> None:
    gate = Gate(monkeypatch)
    s = opened(big_path)
    panel.set_session(s)
    panel.set_options({"text": "toshkent"})
    panel.run_search()
    qtbot.waitUntil(lambda: panel.hit_count_shown() > 0, timeout=TIMEOUT)
    shown = panel.hit_count_shown()
    # Esc in the search field stops it
    qtbot.keyClick(panel.query_combo.lineEdit(), Qt.Key.Key_Escape)
    gate.open()
    with qtbot.waitSignal(panel.search_finished, timeout=TIMEOUT) as blocker:
        pass
    assert blocker.args[1] == "cancelled"
    st = panel.state()
    assert st.status == "cancelled" and panel.hit_count_shown() >= shown > 0
    assert "To'xtatildi" in panel.summary_text()
    assert not panel.is_running() and not panel.cancel_button.isEnabled()
    assert panel.export_button.isEnabled()
    # the partial list can still be opened as a record set (it is not capped)
    assert panel.open_button.isEnabled()
    with qtbot.waitSignal(panel.records_opened, timeout=TIMEOUT):
        panel.open_as_records()
    assert s.view_len() == len({h.record for h in st.hits})
    assert app.errors == []


def test_a_new_search_cancels_the_running_one(qtbot, app, opened, panel, big_path, monkeypatch) -> None:
    gate = Gate(monkeypatch)
    s = opened(big_path)
    panel.set_session(s)
    panel.set_options({"text": "toshkent"})
    first = panel.run_search()
    qtbot.waitUntil(lambda: panel.hit_count_shown() > 0, timeout=TIMEOUT)
    panel.set_options({"text": "user123", "whole_word": True})
    second = panel.run_search()
    assert first.cancel_requested and second is not first
    gate.open()
    with qtbot.waitSignal(panel.search_finished, timeout=TIMEOUT) as blocker:
        pass
    assert blocker.args[1] == "done"
    assert app.tasks.wait_idle(30)
    assert first.state == "cancelled"
    st = panel.state()
    assert [h.text for h in st.hits] == ["user123"] and st.hit_count == 1


def test_closing_the_tab_cancels_its_search(qtbot, app, opened, panel, big_path, monkeypatch) -> None:
    gate = Gate(monkeypatch)
    s = opened(big_path)
    panel.set_session(s)
    panel.set_options({"text": "toshkent"})
    task = panel.run_search()
    qtbot.waitUntil(lambda: panel.hit_count_shown() > 0, timeout=TIMEOUT)
    s.close()
    assert task.cancel_requested
    assert panel.session() is None and panel.state(s) is None
    assert panel.hit_count_shown() == 0
    gate.open()
    assert app.tasks.wait_idle(30)
    assert app.errors == []


# ----------------------------------------------------------------------------------------
# sessions and sources
# ----------------------------------------------------------------------------------------
def test_results_are_kept_per_session(qtbot, app, opened, panel, small_path, tmp_path) -> None:
    a = opened(small_path)
    b = opened(write_records(tmp_path / "other.json", 40))
    panel.set_session(a)
    run(qtbot, panel, "buxoro")
    a_hits = list(panel.state().hits)
    assert a_hits
    panel.set_session(b)
    assert panel.hit_count_shown() == 0 and panel.state(b).status == "idle"
    run(qtbot, panel, "user3", whole_word=True)
    assert [h.record for h in panel.state().hits] == [3]
    panel.set_session(a)
    assert panel.state().hits == a_hits and panel.hit_count_shown() == len(a_hits)
    assert panel.summary_text() == panel.state().result.summary()
    panel.set_session(None)
    assert panel.hit_count_shown() == 0 and not panel.search_button.isEnabled()
    panel.set_session(b)
    assert panel.hit_count_shown() == 1


def test_filtered_view_is_the_source(qtbot, app, opened, panel, big_path, tmp_path, lang_uz) -> None:
    s = opened(big_path)
    s.set_filter(FilterSpec("grp == 1 and amount < 50000"))
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    view = [s.record_at(i) for i in range(s.view_len())]
    assert 0 < len(view) < N
    panel.set_session(s)
    assert "joriy ko'rinish" in panel.source_label.full_text()
    run(qtbot, panel, "toshkent")
    st = panel.state()
    ref = search_mod.reference_search(s.doc, st.opts, view)
    assert st.hits == ref and ref
    assert all(h.record in set(view) for h in st.hits[:200])
    assert st.source is not None                     # a snapshot owned by the panel
    # the view changes: the search's source snapshot is unaffected (export uses it)
    s.reset_view()
    qtbot.waitUntil(lambda: s.view_is_base, timeout=TIMEOUT)
    with qtbot.waitSignal(panel.export_finished, timeout=TIMEOUT) as blocker:
        panel.export_hits_to(str(tmp_path / "filtered.json"))
    assert blocker.args[1] == len(ref)
    assert "barcha yozuvlar" in panel.source_label.full_text()


def test_snapshot_copies_and_detects_a_closed_view() -> None:
    rs = RecordSet.from_indices(range(20_000))
    copy = snapshot_recordset(rs)
    assert list(copy) == list(range(20_000)) and copy.complete
    copy.close()
    rs.close()

    class ClosedMeanwhile:
        """A set the session closed while it was copied: it reads as empty."""

        closed = True

        def __len__(self) -> int:
            return 3

        def iter_chunks(self):
            return iter(())

    with pytest.raises(UserInputError) as info:
        snapshot_recordset(ClosedMeanwhile())  # type: ignore[arg-type]
    assert info.value.key == "gui.search.view_changed"


def test_reopen_marks_results_stale(qtbot, app, opened, panel, small_path, lang_uz) -> None:
    s = opened(small_path)
    panel.set_session(s)
    run(qtbot, panel, "buxoro")
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen()
    assert panel.hit_count_shown() == 0 and panel.state().status == "stale"
    assert "qayta ochildi" in panel.summary_text()
    run(qtbot, panel, "buxoro")
    assert panel.hit_count_shown() > 0


# ----------------------------------------------------------------------------------------
# inputs, menu, keyboard
# ----------------------------------------------------------------------------------------
def test_invalid_inputs_are_shown_inline(qtbot, app, opened, panel, small_path, lang_uz) -> None:
    s = opened(small_path)
    panel.set_session(s)
    panel.set_options({"text": ""})
    assert panel.error_text() == ""                      # an empty field is not an error while typing
    assert panel.run_search() is None
    assert "bo'sh" in panel.error_text().lower()
    panel.set_options({"text": "(abc", "mode": "regex"})
    assert panel.error_text() and panel.query_combo.lineEdit().property("invalid")
    assert panel.run_search() is None
    panel.set_options({"text": "> abc", "mode": "number"})
    assert panel.error_text()
    assert not panel.translit_check.isEnabled() and not panel.case_check.isEnabled()
    panel.set_options({"text": "buxoro", "mode": "plain", "path": "a[", "translit": False})
    assert panel.error_text() and panel.path_edit.property("invalid")
    assert panel.run_search() is None
    panel.set_options({"path": "contact.city"})
    assert panel.error_text() == "" and not panel.path_edit.property("invalid")
    assert panel.translit_check.isEnabled()
    # Enter in the path field runs the search
    with qtbot.waitSignal(panel.search_finished, timeout=TIMEOUT):
        qtbot.keyClick(panel.path_edit, Qt.Key.Key_Return)
    assert panel.hit_count_shown() > 0
    assert all(h.path == ("contact", "city") for h in panel.state().hits)


def test_no_document(qtbot, app, panel, lang_uz) -> None:
    panel.set_options({"text": "x"})
    assert panel.run_search() is None
    assert "fayl oching" in panel.error_text()
    assert not panel.export_button.isEnabled() and not panel.open_button.isEnabled()


def test_focus_search_takes_the_first_line(qtbot, app, panel) -> None:
    panel.focus_search("  Toshkent\nikkinchi qator ")
    assert panel.query_text() == "Toshkent"
    panel.focus_search(None)
    assert panel.query_text() == "Toshkent"


def test_context_menu_and_copy(qtbot, app, opened, panel, small_path) -> None:
    s = opened(small_path)
    panel.set_session(s)
    run(qtbot, panel, "Buxoro", path="contact.city")
    menu = panel.hit_menu(1)
    names = [a.objectName() for a in menu.actions() if a.objectName()]
    assert names == ["show_in_tree", "copy_path", "copy_text", "copy_record", "copy_rows"]
    hit = panel.hit_at(1)
    action = {a.objectName(): a for a in menu.actions()}
    action["copy_path"].trigger()
    assert QGuiApplication.clipboard().text() == f"[{hit.record}].contact.city"
    action["copy_record"].trigger()
    assert QGuiApplication.clipboard().text() == f"#{hit.record}"
    action["copy_text"].trigger()
    assert QGuiApplication.clipboard().text() == hit.text
    panel.grid.select_all()
    qtbot.keyClick(panel.grid, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    lines = QGuiApplication.clipboard().text().split("\n")
    assert len(lines) == panel.hit_count_shown()
    assert lines[0].split("\t")[0] == str(panel.hit_at(0).record)
    with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT):
        action["show_in_tree"].trigger()
    assert s.current.record == hit.record
