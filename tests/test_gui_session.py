"""DocSession / AppContext (jsonreshape/gui/session.py, context.py; ARCHITECTURE §11.6).

Open → opened → index_finished (and the cached-index shortcut), live record count while
indexing, failed opens, the view pipeline (expression / builder / JMESPath filters, quick
filters with one or two passes, partial results, sort after filter, a newer request
cancelling an older one, errors keeping the previous view), show_records / reset_view,
single documents, position_of, node references for every format, history, filter by
value, bookmarks, external change detection, reopen and close.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from jsonreshape import reader as reader_mod
from jsonreshape.errors import FileFormatError, UserInputError
from jsonreshape.gui import session as session_mod
from jsonreshape.gui.context import AppContext
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.session import HISTORY_MAX, DocSession, FilterSpec, NodeRef, find_position, same_node
from jsonreshape.paths import parse_location
from jsonreshape.query import Condition, Group
from jsonreshape.reader import Document
from jsonreshape.recordset import RecordSet
from jsonreshape.sortkeys import SortKey

pytestmark = pytest.mark.gui

TIMEOUT = 30000   # ms: generous for loaded machines


# ----------------------------------------------------------------------------------------
# fixtures and helpers
# ----------------------------------------------------------------------------------------
def make_records(n: int) -> list[dict]:
    return [{"id": i, "name": f"n{i % 7}", "age": i % 50, "grp": i % 3, "tags": [f"t{i % 4}", "x"],
             "nested": {"v": i * 2}} for i in range(n)]


RECS = make_records(300)


@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(20)   # faster hand-over than the default 100 ms
    ctx.errors = []
    ctx.error_reported.connect(ctx.errors.append)
    yield ctx
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def make(app):
    made: list[DocSession] = []

    def factory(path, **kw) -> DocSession:
        s = DocSession(app, path, **kw)
        made.append(s)
        return s

    yield factory
    for s in made:
        s.close()
    assert app.tasks.wait_idle(30)


def open_ready(qtbot, make, path, **kw) -> DocSession:
    kw.setdefault("use_cache", False)
    s = make(path, **kw)
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.start()
    assert s.state == "ready"
    return s


def wait_view(qtbot, s: DocSession) -> None:
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)


def view_records(s: DocSession) -> list[int]:
    return [s.record_at(i) for i in range(s.view_len())]


def write_json(path: Path, value) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.fixture
def array_file(tmp_path: Path) -> Path:
    return write_json(tmp_path / "records.json", RECS)


def big_file(path: Path, n: int = 200_000) -> Path:
    path.write_bytes(b"[" + b",".join(b'{"i":%d,"g":%d}' % (i, i % 2) for i in range(n)) + b"]")
    return path


class Recorder:
    """Records every signal of a session as (name, args)."""

    NAMES = ("state_changed", "opened", "open_failed", "index_finished", "count_changed", "view_changed",
             "view_busy_changed", "current_changed", "reveal_requested", "file_changed", "warnings_changed",
             "history_changed", "bookmarks_changed")

    def __init__(self, s: DocSession) -> None:
        self.events: list[tuple] = []
        for name in self.NAMES:
            getattr(s, name).connect(lambda *args, n=name: self.events.append((n, args)))

    def names(self) -> list[str]:
        return [e[0] for e in self.events]


def slow_filter(monkeypatch, delay: float = 0.01, block_bytes: int = 64 * 1024) -> None:
    """filter_records with tiny blocks, every batch reported and a pause per batch."""
    orig = session_mod.filter_records

    def wrapped(doc, flt, source=None, *, progress=None, cancel=None, on_batch=None, **kw):
        def batch(rs):
            on_batch(rs)
            time.sleep(delay)

        return orig(doc, flt, source, progress=progress, cancel=cancel, on_batch=batch if on_batch else None,
                    batch_interval=0.0, block_bytes=block_bytes)

    monkeypatch.setattr(session_mod, "filter_records", wrapped)


def slow_index(monkeypatch, delay: float = 0.004) -> None:
    """Index in 32 KiB pieces with a pause per batch of spans (observable live growth)."""
    orig = Document._append_spans

    def slow(self, writer, out):
        orig(self, writer, out)
        time.sleep(delay)

    monkeypatch.setattr(Document, "_append_spans", slow)
    monkeypatch.setattr(reader_mod, "FEED_BYTES", 32 * 1024)


# ----------------------------------------------------------------------------------------
# open / index
# ----------------------------------------------------------------------------------------
def test_open_index_signal_order(qtbot, make, array_file: Path, lang_uz) -> None:
    s = make(array_file, use_cache=False)
    rec = Recorder(s)
    assert s.state == "new" and s.doc is None and s.view_len() == 0
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.start()
    names = rec.names()
    states = [e[1][0] for e in rec.events if e[0] == "state_changed"]
    assert states == ["opening", "indexing", "ready"]
    assert names.index("opened") < names.index("index_finished")
    after = names[names.index("index_finished"):]
    assert after[:3] == ["index_finished", "count_changed", "warnings_changed"]
    assert s.doc is not None and s.doc.record_count == 300
    assert s.view_len() == 300 and s.view_is_base
    assert s.record_at(299) == 299
    with pytest.raises(IndexError):
        s.record_at(300)
    assert s.view_label() == "Barcha yozuvlar"
    assert s.name == "records.json" and s.path == os.path.abspath(array_file)
    kinds = [t.kind for t in s.app.tasks.tasks(owner=s, include_silent=True)]
    assert "open" not in kinds and "index" not in kinds
    s.start()   # a second start is ignored
    assert s.state == "ready"


def test_cached_index_goes_straight_to_ready(qtbot, make, array_file: Path) -> None:
    first = open_ready(qtbot, make, array_file, use_cache=True)
    first.close()
    s = make(array_file, use_cache=True)
    rec = Recorder(s)
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.start()
    states = [e[1][0] for e in rec.events if e[0] == "state_changed"]
    assert states == ["opening", "ready"]
    assert s.doc.index_from_cache
    assert rec.names().index("opened") < rec.names().index("index_finished")


def test_live_count_growth(qtbot, make, tmp_path: Path, monkeypatch) -> None:
    slow_index(monkeypatch)
    monkeypatch.setattr(DocSession, "COUNT_POLL_MS", 25)
    path = big_file(tmp_path / "big.json")
    s = make(path, use_cache=False)
    seen: list[tuple[str, int, int]] = []
    s.count_changed.connect(lambda: seen.append((s.state, s.doc.record_count if s.doc else 0, s.view_len())))
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.start()
    growing = [(c, v) for state, c, v in seen if state == "indexing" and 0 < c < 200_000]
    assert len({c for c, _ in growing}) >= 3, seen[:20]
    counts = [c for _, c in growing]
    assert counts == sorted(counts)
    assert all(v == c for c, v in growing)          # the base view follows the live count
    assert s.view_len() == 200_000 and seen[-1][1] == 200_000


def test_failed_open_missing_and_empty(qtbot, make, tmp_path: Path, app) -> None:
    s = make(tmp_path / "missing.json")
    rec = Recorder(s)
    with qtbot.waitSignal(s.open_failed, timeout=TIMEOUT) as blocker:
        s.start()
    assert s.state == "failed"
    assert s.open_error is blocker.args[0]
    assert "opened" not in rec.names()
    assert app.errors == []                          # emitted only: the tab decides
    empty = tmp_path / "empty.json"
    empty.write_bytes(b"   ")
    s2 = make(empty)
    with qtbot.waitSignal(s2.open_failed, timeout=TIMEOUT) as blocker:
        s2.start()
    assert isinstance(blocker.args[0], FileFormatError)
    assert s2.view_len() == 0


def test_broken_json_keeps_records_before_error(qtbot, make, tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_bytes(b'[{"a": 1}, {"a": 2}, {"a": 3}, {"a": oops}]')
    s = open_ready(qtbot, make, path)
    assert s.doc.error is not None
    assert s.view_len() == 3
    assert s.node(NodeRef(2, ("a",))).value() == 3


def test_index_cancelled_by_user_fails_the_session(qtbot, make, tmp_path: Path, monkeypatch) -> None:
    slow_index(monkeypatch)
    s = make(big_file(tmp_path / "big.json"), use_cache=False)
    s.start()
    qtbot.waitUntil(lambda: any(t.kind == "index" for t in s.app.tasks.tasks(owner=s)), timeout=TIMEOUT)
    task = next(t for t in s.app.tasks.tasks(owner=s) if t.kind == "index")
    with qtbot.waitSignal(s.open_failed, timeout=TIMEOUT) as blocker:
        task.cancel()
    assert s.state == "failed"
    assert type(blocker.args[0]).__name__ == "Cancelled"


# ----------------------------------------------------------------------------------------
# view pipeline
# ----------------------------------------------------------------------------------------
def test_filter_expression(qtbot, make, array_file: Path, lang_uz) -> None:
    s = open_ready(qtbot, make, array_file)
    rec = Recorder(s)
    s.set_filter(FilterSpec("age > 40"))
    assert s.view_busy
    wait_view(qtbot, s)
    assert view_records(s) == [r["id"] for r in RECS if r["age"] > 40]
    assert s.view_label() == "Filtr: age > 40"
    busy = [e[1][0] for e in rec.events if e[0] == "view_busy_changed"]
    assert busy == [True, False]
    assert "view_changed" in rec.names()
    assert s.app.errors == []


def test_filter_builder(qtbot, make, array_file: Path) -> None:
    s = open_ready(qtbot, make, array_file)
    group = Group(items=[Condition("grp", "eq", 1), Condition("name", "eq", "n3")])
    s.set_filter(FilterSpec(text="", builder=group.to_dict()))
    wait_view(qtbot, s)
    assert view_records(s) == [r["id"] for r in RECS if r["grp"] == 1 and r["name"] == "n3"]
    assert group.to_text() in s.view_label()


def count_kinds(app, kind: str) -> list:
    got: list = []
    app.tasks.task_added.connect(lambda task: got.append(task) if task.kind == kind else None)
    return got


def test_expr_and_quick_filters_single_pass(qtbot, make, array_file: Path, app) -> None:
    s = open_ready(qtbot, make, array_file)
    s.set_filter(FilterSpec("age >= 10"))
    wait_view(qtbot, s)
    filters = count_kinds(app, "filter")
    s.set_quick_filter("grp", "grp == 2")
    wait_view(qtbot, s)
    assert len(filters) == 1                          # one combined pass
    assert view_records(s) == [r["id"] for r in RECS if r["age"] >= 10 and r["grp"] == 2]
    assert s.quick_filters == {"grp": "grp == 2"}
    s.set_quick_filter("name", 'name == "n1"')
    wait_view(qtbot, s)
    assert view_records(s) == [r["id"] for r in RECS if r["age"] >= 10 and r["grp"] == 2 and r["name"] == "n1"]
    s.set_quick_filter("grp", None)
    s.set_quick_filter("name", "")
    wait_view(qtbot, s)
    assert s.quick_filters == {}
    assert view_records(s) == [r["id"] for r in RECS if r["age"] >= 10]
    s.set_filter(None)
    wait_view(qtbot, s)
    assert s.view_is_base and s.view_len() == 300


def test_jmespath_and_quick_filter_two_passes(qtbot, make, array_file: Path, app) -> None:
    s = open_ready(qtbot, make, array_file)
    filters = count_kinds(app, "filter")
    s.set_filter(FilterSpec("age > `10` && grp != `0`", lang="jmespath"))
    wait_view(qtbot, s)
    assert len(filters) == 1
    s.set_quick_filter("name", 'name == "n2"')
    wait_view(qtbot, s)
    assert len(filters) == 3                          # JMESPath pass + quick-filter pass
    assert view_records(s) == [r["id"] for r in RECS if r["age"] > 10 and r["grp"] != 0 and r["name"] == "n2"]
    # quick filters alone: one expression pass
    s.set_filter(None)
    wait_view(qtbot, s)
    assert view_records(s) == [r["id"] for r in RECS if r["name"] == "n2"]


def test_partial_results_then_final(qtbot, make, tmp_path: Path, monkeypatch) -> None:
    slow_filter(monkeypatch)
    s = open_ready(qtbot, make, big_file(tmp_path / "big.json"))
    views: list[tuple] = []
    s.view_changed.connect(lambda: views.append((s.view_recordset(), s.view_recordset().complete, s.view_len())))
    s.set_filter(FilterSpec("g == 0"))
    wait_view(qtbot, s)
    final = s.view_recordset()
    assert final.complete and len(final) == 100_000
    assert views[0][0] is final and views[0][1] is False   # the growing set became the view first
    assert 0 < views[0][2] < 100_000
    assert len(views) == 1                                  # installed once, then it only grew
    assert s.record_at(0) == 0 and s.record_at(99_999) == 199_998


def test_sort_after_filter_and_sort_only_change(qtbot, make, array_file: Path, app, lang_uz) -> None:
    s = open_ready(qtbot, make, array_file)
    s.set_filter(FilterSpec("grp == 1"))
    s.set_sort([SortKey("age", descending=True)])
    wait_view(qtbot, s)
    expected = sorted((r for r in RECS if r["grp"] == 1), key=lambda r: -r["age"])   # stable
    assert view_records(s) == [r["id"] for r in expected]
    assert "Filtr: grp == 1" in s.view_label() and "Tartib:" in s.view_label()
    filters, sorts = count_kinds(app, "filter"), count_kinds(app, "sort")
    s.sort_by_field(("name",))
    wait_view(qtbot, s)
    assert filters == [] and len(sorts) == 1              # the filter result was reused
    expected = sorted((r for r in RECS if r["grp"] == 1), key=lambda r: r["name"])
    assert view_records(s) == [r["id"] for r in expected]
    assert [k.path for k in s.sort_keys] == ["name"]
    s.set_sort(None)
    wait_view(qtbot, s)
    assert filters == [] and len(sorts) == 1
    assert view_records(s) == [r["id"] for r in RECS if r["grp"] == 1]


def test_newer_filter_cancels_older(qtbot, make, tmp_path: Path, monkeypatch, app) -> None:
    slow_filter(monkeypatch, delay=0.02)
    s = open_ready(qtbot, make, big_file(tmp_path / "big.json", 100_000))
    a_views: list = []
    s.view_changed.connect(lambda: a_views.append(s.view_recordset()))
    filters = count_kinds(app, "filter")
    s.set_filter(FilterSpec("g == 0"))
    qtbot.waitUntil(lambda: len(a_views) >= 1, timeout=TIMEOUT)   # A's partial result is shown
    a_partial = a_views[0]
    assert not a_partial.complete
    later: list = []
    s.view_changed.connect(lambda: later.append(s.view_recordset()))
    s.set_filter(FilterSpec("g == 1"))
    wait_view(qtbot, s)
    qtbot.wait(300)
    assert all(rs is not a_partial for rs in later)
    assert filters[0].state == "cancelled"
    final = s.view_recordset()
    assert final.complete and len(final) == 50_000
    assert s.record_at(0) == 1 and s.record_at(49_999) == 99_999
    assert s.filter.text == "g == 1"
    assert app.errors == []


def test_bad_expression_keeps_view(qtbot, make, array_file: Path, app) -> None:
    s = open_ready(qtbot, make, array_file)
    s.set_filter(FilterSpec("age > 40"))
    wait_view(qtbot, s)
    before = s.view_recordset()
    with qtbot.waitSignal(s.view_busy_changed, timeout=TIMEOUT) as blocker:
        s.set_filter(FilterSpec("age >>> 1"))
    assert blocker.args == [False]
    assert len(app.errors) == 1 and type(app.errors[0]).__name__ == "QuerySyntaxError"
    assert s.view_recordset() is before and s.filter.text == "age > 40"
    s.set_quick_filter("x", "((")
    assert len(app.errors) == 2 and s.quick_filters == {}


def test_runtime_filter_error_restores_previous_view(qtbot, make, array_file: Path, app) -> None:
    s = open_ready(qtbot, make, array_file)
    s.set_sort([SortKey("age")])
    wait_view(qtbot, s)
    before = s.view_recordset()
    before_records = view_records(s)
    busy: list = []
    s.view_busy_changed.connect(busy.append)
    s.set_filter(FilterSpec("abs(tags) > `1`", lang="jmespath"))   # fails on every record
    wait_view(qtbot, s)
    assert busy == [True, False]
    assert len(app.errors) == 1 and app.errors[0].key == "recordset.error.eval_failed"
    assert s.view_recordset() is before and view_records(s) == before_records
    assert s.filter is None and [k.path for k in s.sort_keys] == ["age"]


def test_show_records_and_reset_view(qtbot, make, array_file: Path, monkeypatch) -> None:
    monkeypatch.setattr(DocSession, "RETIRE_DELAY_MS", 100)
    s = open_ready(qtbot, make, array_file)
    s.set_filter(FilterSpec("age > 10"))
    s.set_sort([SortKey("id", descending=True)])
    wait_view(qtbot, s)
    sorted_rs = s.view_recordset()
    hits = RecordSet.from_indices([5, 3, 250], kind="search")
    with qtbot.waitSignal(s.view_changed, timeout=TIMEOUT):
        s.show_records(hits, "Qidiruv: x")
    assert view_records(s) == [5, 3, 250]
    assert s.view_label() == "Qidiruv: x"
    assert s.filter is None and s.quick_filters == {} and s.sort_keys == []
    qtbot.waitUntil(lambda: sorted_rs.closed, timeout=TIMEOUT)    # replaced sets are closed later
    assert not hits.closed
    with qtbot.waitSignal(s.view_changed, timeout=TIMEOUT):
        s.reset_view()
    assert s.view_is_base and s.view_len() == 300
    assert s.view_label() != "Qidiruv: x"
    qtbot.waitUntil(lambda: hits.closed, timeout=TIMEOUT)
    # a filter after show_records works on the shown records (review finding M5: it used to
    # restart silently from all records); reset_view goes back to all records
    s.show_records(RecordSet.from_indices([1, 2, 7]), "r")
    s.set_filter(FilterSpec("id < 5"))
    wait_view(qtbot, s)
    assert view_records(s) == [1, 2]
    assert s.view_label().startswith("r \N{MIDDLE DOT} ") and "id < 5" in s.view_label()
    s.reset_view()
    assert s.view_is_base and s.view_len() == 300 and s.shown_records is None


def test_single_document_refuses_filter_and_sort(qtbot, make, tmp_path: Path) -> None:
    s = open_ready(qtbot, make, write_json(tmp_path / "single.json", "just a string"))
    assert s.doc.kind == "single"
    for call in (lambda: s.set_filter(FilterSpec("a == 1")), lambda: s.set_sort([SortKey("a")]),
                 lambda: s.set_quick_filter("a", "a == 1"), lambda: s.sort_by_field(("a",))):
        with pytest.raises(UserInputError) as info:
            call()
        assert info.value.key == "gui.session.single_no_view"
    assert s.filter_by_value(NodeRef(None, ()), "x") is False
    assert s.filter_by_value(NodeRef(0, ()), "x") is False
    assert s.node(NodeRef(None, ())).value() == "just a string"


def test_filter_set_before_open_is_applied_when_ready(qtbot, make, array_file: Path) -> None:
    s = make(array_file, use_cache=False)
    s.set_filter(FilterSpec("grp == 0"))
    s.set_sort([SortKey("age", descending=True)])
    s.start()
    qtbot.waitUntil(lambda: s.state == "ready" and not s.view_busy and s.view_len() == 100, timeout=TIMEOUT)
    assert view_records(s)[:2] == [99, 249]    # age 49 and grp 0, stable order


# ----------------------------------------------------------------------------------------
# positions
# ----------------------------------------------------------------------------------------
def test_position_of(qtbot, make, array_file: Path) -> None:
    s = open_ready(qtbot, make, array_file)
    got: list = []
    s.position_of(42, got.append)
    assert got == []                                   # always asynchronous
    qtbot.waitUntil(lambda: got == [42], timeout=TIMEOUT)
    s.position_of(300, got.append)
    qtbot.waitUntil(lambda: len(got) == 2, timeout=TIMEOUT)
    assert got[1] is None
    s.set_filter(FilterSpec("grp == 2"))                # ascending result: binary search
    wait_view(qtbot, s)
    got.clear()
    for record in (2, 5, 299, 3):
        s.position_of(record, got.append)
    qtbot.waitUntil(lambda: len(got) == 4, timeout=TIMEOUT)
    assert sorted(got, key=str) == sorted([0, 1, 99, None], key=str)
    s.set_sort([SortKey("age", descending=True)])      # not ascending: chunk scan
    wait_view(qtbot, s)
    order = view_records(s)
    got.clear()
    s.position_of(order[37], got.append)
    s.position_of(1, got.append)
    qtbot.waitUntil(lambda: len(got) == 2, timeout=TIMEOUT)
    assert set(map(str, got)) == {"37", "None"}
    got.clear()
    s.position_of(order[37], got.append)               # cached
    qtbot.waitUntil(lambda: got == [37], timeout=TIMEOUT)


def test_find_position_alignment() -> None:
    # a value whose bytes straddle two slots must not match: [0x0100, 0x0200...]
    rs = RecordSet.from_indices([256, 2, 1 << 40, 7, 3])
    assert find_position(rs, 7) == 3
    assert find_position(rs, 1 << 40) == 2
    assert find_position(rs, 1) is None
    assert find_position(rs, -1) is None
    straddle = RecordSet.from_indices([0x0100000000000000, 0x00000000000000FF, 9])
    # bytes of the two first values: 00..00 01 | FF 00..00 -> 0x01FF... is not in the set
    assert find_position(straddle, 0xFF01) is None
    assert find_position(straddle, 9) == 2
    asc = RecordSet.from_indices(range(0, 1000, 3))
    assert find_position(asc, 999) == 333 and find_position(asc, 998) is None


# ----------------------------------------------------------------------------------------
# node references
# ----------------------------------------------------------------------------------------
def check_round_trip(s: DocSession, ref: NodeRef, abs_path: tuple) -> None:
    assert s.absolute_path(ref) == abs_path
    back = s.ref_for_path(abs_path)
    assert same_node(back, ref), (back, ref)
    assert back.ipath is not None and len(back.ipath) == len(back.path)
    assert s.node(back) == s.node(ref)
    loc = parse_location(s.location_text(ref))
    assert same_node(s.ref_for_location(loc), ref)


def test_refs_array_and_jsonl(qtbot, make, array_file: Path, tmp_path: Path) -> None:
    s = open_ready(qtbot, make, array_file)
    check_round_trip(s, NodeRef(3, ("tags", 1)), (3, "tags", 1))
    check_round_trip(s, NodeRef(299, ()), (299,))
    check_round_trip(s, NodeRef(0, ("nested", "v")), (0, "nested", "v"))
    assert s.ref_for_path("[7].nested") == NodeRef(7, ("nested",), (5,))
    assert s.ref_for_path("/7/tags/0") == NodeRef(7, ("tags", 0), (4, 0))   # pointer text: "0" -> index
    assert s.ref_for_path(()) == NodeRef(None, (), ())
    with pytest.raises(UserInputError):
        s.ref_for_path((300,))
    with pytest.raises(UserInputError):
        s.ref_for_path((3, "nope"))
    jl = tmp_path / "r.jsonl"
    jl.write_text("".join(json.dumps(r) + "\n" for r in RECS[:20]), encoding="utf-8")
    s2 = open_ready(qtbot, make, jl)
    assert s2.doc.kind == "jsonl"
    check_round_trip(s2, NodeRef(19, ("name",)), (19, "name"))


def test_refs_keyed_with_duplicates(qtbot, make, tmp_path: Path) -> None:
    path = tmp_path / "keyed.json"
    path.write_bytes(b'{"a": {"v": [1, 2]}, "b": {"v": [3]}, "a": {"v": [4, 5, 6]}, "c": {"v": []}}')
    s = open_ready(qtbot, make, path)
    assert s.doc.kind == "keyed" and s.view_len() == 4
    check_round_trip(s, NodeRef(1, ("v", 0)), ("b", "v", 0))
    check_round_trip(s, NodeRef(2, ("v", 2)), ("a", "v", 2))   # the last "a"
    assert s.absolute_path(NodeRef(0, ("v",))) == ("a", "v")
    # the first duplicate's path resolves to the last one (dict semantics)
    assert s.ref_for_path(("a", "v")).record == 2
    assert s.node(NodeRef(0, ("v",))).value() == [1, 2]       # record refs keep the duplicate
    assert s.location_text(NodeRef(0, ("v", 1))) == "#0.v[1]"


def test_refs_wrapped_array_and_object_containers(qtbot, make, tmp_path: Path) -> None:
    w = write_json(tmp_path / "wrapped.json", {"meta": {"v": 1}, "data": RECS[:30], "tail": {"n": 30}})
    s = open_ready(qtbot, make, w)
    assert s.doc.kind == "wrapped" and s.doc.record_path == ("data",)
    check_round_trip(s, NodeRef(12, ("tags", 0)), ("data", 12, "tags", 0))
    check_round_trip(s, NodeRef(None, ("meta", "v")), ("meta", "v"))
    check_round_trip(s, NodeRef(None, ("data",)), ("data",))
    assert s.ref_for_location(parse_location("/data/3/id")) == NodeRef(3, ("id",), (0,))
    assert s.node(NodeRef(None, ("tail", "n"))).value() == 30
    o = write_json(tmp_path / "wobj.json", {"meta": 1, "items": {"x": {"q": [1]}, "y": {"q": [2, 3]}}})
    s2 = open_ready(qtbot, make, o, record_path="items")
    assert s2.doc.member_records
    check_round_trip(s2, NodeRef(1, ("q", 1)), ("items", "y", "q", 1))
    check_round_trip(s2, NodeRef(None, ("meta",)), ("meta",))


def test_refs_single_document(qtbot, make, tmp_path: Path) -> None:
    s = open_ready(qtbot, make, write_json(tmp_path / "one.json", {"a": {"b": [10, 20]}, "c": "x"}),
                   format="single")
    assert s.doc.kind == "single"
    check_round_trip(s, NodeRef(None, ("a", "b", 1)), ("a", "b", 1))
    assert s.ref_for_location(parse_location("#0.a.b[0]")) == NodeRef(None, ("a", "b", 0), (0, 0, 0))
    with pytest.raises(UserInputError):
        s.ref_for_location(parse_location("#1"))
    assert s.location_text(NodeRef(None, ())) == "$"


def test_node_and_resolve_with_ipath(qtbot, make, tmp_path: Path) -> None:
    path = tmp_path / "dups.json"
    path.write_bytes(b'[{"k": 1, "arr": [{"z": 0}, {"z": 1}], "k": 2}, {"k": 3}]')
    s = open_ready(qtbot, make, path)
    ref = s.resolve(NodeRef(0, ("arr", "1", "z")))
    assert ref == NodeRef(0, ("arr", 1, "z"), (1, 1, 0))
    assert s.node(ref).value() == 1
    # "k" resolves to the last duplicate; ipath can address the first one (tree identity)
    assert s.resolve(NodeRef(0, ("k",))).ipath == (2,)
    assert s.node(NodeRef(0, ("k",))).value() == 2
    assert s.node(NodeRef(0, ("k",), (0,))).value() == 1
    # a stale ipath falls back to the keys
    assert s.node(NodeRef(0, ("arr", 0), (0, 0))).value() == {"z": 0}
    assert s.node(NodeRef(0, ("arr", 0), (9, 9))).value() == {"z": 0}
    with pytest.raises(UserInputError) as info:
        s.node(NodeRef(1, ("missing",)))
    assert info.value.key == "reader.path_not_found"
    with pytest.raises(UserInputError) as info:
        s.node(NodeRef(5, ()))
    assert info.value.key == "gui.session.record_not_found"


# ----------------------------------------------------------------------------------------
# selection, history
# ----------------------------------------------------------------------------------------
def test_select_and_history(qtbot, make, array_file: Path) -> None:
    s = open_ready(qtbot, make, array_file)
    rec = Recorder(s)
    origin = object()
    s.select(NodeRef(1, ()), origin=origin)
    assert rec.events[-1] == ("current_changed", (NodeRef(1, ()), origin))
    n = len(rec.events)
    s.select(NodeRef(1, (), (0,)), origin=origin)                 # same node: skipped, ipath kept
    assert len(rec.events) == n and s.current.ipath == (0,)
    s.select(NodeRef(1, ()), reveal=True)                         # same node, reveal: both signals
    assert [e[0] for e in rec.events[n:]] == ["current_changed", "reveal_requested"]
    assert not s.can_back and not s.can_forward
    for i in range(2, HISTORY_MAX + 10):
        s.select(NodeRef(i, ()))
    assert len(s.history()) == HISTORY_MAX
    assert s.history()[-1] == NodeRef(HISTORY_MAX + 9, ())
    s.select(NodeRef(0, ("x",)), history=False)                   # not recorded
    assert s.history()[-1] == NodeRef(HISTORY_MAX + 9, ())
    steps = 0
    while s.can_back:
        s.back()
        steps += 1
    assert steps == HISTORY_MAX - 1
    assert s.current == s.history()[0] and not s.can_back
    s.back()                                                      # at the start: nothing
    assert s.current == s.history()[0]
    rec.events.clear()
    s.forward()
    assert [e[0] for e in rec.events if e[0] in ("current_changed", "reveal_requested")] == \
        ["current_changed", "reveal_requested"]
    assert rec.events[[e[0] for e in rec.events].index("current_changed")][1] == (s.history()[1], None)
    s.select(NodeRef(7, ("tags",)))                               # truncates the forward entries
    assert not s.can_forward and s.can_back
    assert s.history()[-2:] == [s.history()[1], NodeRef(7, ("tags",))]
    assert len(s.history()) == 3
    s.select(None)
    assert s.current is None and len(s.history()) == 3


# ----------------------------------------------------------------------------------------
# actions
# ----------------------------------------------------------------------------------------
def test_filter_by_value_expr_builder_and_jmespath(qtbot, make, array_file: Path) -> None:
    s = open_ready(qtbot, make, array_file)
    assert s.filter_by_value(NodeRef(None, ()), 1) is False
    # no filter: a builder filter is created
    assert s.filter_by_value(NodeRef(4, ("grp",)), 1) is True
    wait_view(qtbot, s)
    assert s.filter.builder is not None and s.filter.text == "grp == 1"
    assert view_records(s) == [r["id"] for r in RECS if r["grp"] == 1]
    # with a builder: the builder is extended
    assert s.filter_by_value(NodeRef(4, ("name",)), "n4")
    wait_view(qtbot, s)
    group = Group.from_dict(s.filter.builder)
    assert [c.field for c in group.items] == ["grp", "name"]
    assert view_records(s) == [r["id"] for r in RECS if r["grp"] == 1 and r["name"] == "n4"]
    # a text filter: "(old) and cond"
    s.set_filter(FilterSpec("age < 20"))
    wait_view(qtbot, s)
    assert s.filter_by_value(NodeRef(2, ("tags", 0)), "t2")
    wait_view(qtbot, s)
    assert s.filter.text == '(age < 20) and tags[0] == "t2"' and s.filter.builder is None
    assert view_records(s) == [r["id"] for r in RECS if r["age"] < 20 and r["tags"][0] == "t2"]
    # the whole record value
    s.set_filter(None)
    wait_view(qtbot, s)
    assert s.filter_by_value(NodeRef(5, ()), RECS[5])
    wait_view(qtbot, s)
    assert view_records(s) == [5]
    # JMESPath main filter: a quick filter keyed by the path
    s.set_filter(FilterSpec("age > `5`", lang="jmespath"))
    wait_view(qtbot, s)
    assert s.filter_by_value(NodeRef(8, ("nested", "v")), 16)
    wait_view(qtbot, s)
    assert s.quick_filters == {"nested.v": "nested.v == 16"}
    assert s.filter.lang == "jmespath"
    assert view_records(s) == [8]


def test_sort_by_field(qtbot, make, array_file: Path) -> None:
    s = open_ready(qtbot, make, array_file)
    s.sort_by_field(("nested", "v"), descending=True)
    wait_view(qtbot, s)
    assert [k.path for k in s.sort_keys] == ["nested.v"] and s.sort_keys[0].descending
    assert view_records(s)[:3] == [299, 298, 297]
    s.sort_by_field((), descending=False)
    wait_view(qtbot, s)
    assert s.sort_keys[0].path == "$"


# ----------------------------------------------------------------------------------------
# bookmarks
# ----------------------------------------------------------------------------------------
def test_bookmarks_persisted(qtbot, make, array_file: Path, app, tmp_path: Path) -> None:
    s = make(array_file)
    rec = Recorder(s)
    bm = s.add_bookmark("first", NodeRef(3, ("tags", 0)))
    s.add_bookmark("  ", NodeRef(None, ()))                # empty name: the location text
    assert [b.name for b in s.bookmarks] == ["first", "$"]
    assert s.bookmark_ref(0) == NodeRef(3, ("tags", 0))
    assert s.bookmark_ref(bm) == NodeRef(3, ("tags", 0))
    s.rename_bookmark(1, "root")
    s.rename_bookmark(1, "   ")                            # ignored
    assert rec.names().count("bookmarks_changed") == 3
    # saved by a silent background task (review finding T9: no file lock on the GUI thread)
    qtbot.waitUntil(lambda: not app.state_saving, timeout=TIMEOUT)
    stored = GuiState.load(tmp_path / "gui_state.json").bookmarks(array_file)
    assert [(b.name, b.record, b.path) for b in stored] == [("first", 3, ["tags", 0]), ("root", None, [])]
    s.remove_bookmark(0)
    qtbot.waitUntil(lambda: not app.state_saving, timeout=TIMEOUT)
    stored = GuiState.load(tmp_path / "gui_state.json").bookmarks(array_file)
    assert [b.name for b in stored] == ["root"]
    other = DocSession(app, str(array_file).upper() if os.name == "nt" else array_file)
    try:
        assert [b.name for b in other.bookmarks] == ["root"]
    finally:
        other.close()


# ----------------------------------------------------------------------------------------
# external changes, reopen, close
# ----------------------------------------------------------------------------------------
def test_file_changes_grown_then_changed(qtbot, make, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(DocSession, "CHANGE_POLL_MS", 80)
    path = tmp_path / "log.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in RECS[:50]), encoding="utf-8")
    s = open_ready(qtbot, make, path)
    events: list[str] = []
    s.file_changed.connect(events.append)
    qtbot.wait(300)
    assert events == []
    with open(path, "ab") as f:
        f.write(json.dumps(RECS[50]).encode() + b"\n")
    qtbot.waitUntil(lambda: events == ["grown"], timeout=TIMEOUT)
    qtbot.wait(300)
    assert events == ["grown"]                              # once per growth
    assert any("+" in w or w for w in s.warning_texts())
    with open(path, "ab") as f:
        f.write(json.dumps(RECS[51]).encode() + b"\n")
    qtbot.waitUntil(lambda: events == ["grown", "grown"], timeout=TIMEOUT)
    path.write_text("[1, 2, 3]\n", encoding="utf-8")         # rewritten
    qtbot.waitUntil(lambda: events[-1:] == ["changed"], timeout=TIMEOUT)
    qtbot.wait(300)
    assert events == ["grown", "grown", "changed"]          # stops after "changed"
    assert not s._change_timer.isActive()


def test_reopen_with_another_record_path(qtbot, make, tmp_path: Path) -> None:
    path = write_json(tmp_path / "two.json", {"data": RECS[:40], "other": RECS[:6]})
    s = open_ready(qtbot, make, path)
    assert s.doc.record_path == ("data",) and s.view_len() == 40
    s.set_filter(FilterSpec("id < 20"))
    s.set_sort([SortKey("id", descending=True)])
    wait_view(qtbot, s)
    assert s.view_len() == 20
    s.select(NodeRef(3, ()))
    old_doc = s.doc
    rec = Recorder(s)
    s.reopen(record_path="other")
    assert s.state == "opening" and s.current is None and not s.can_back
    assert s.view_len() == 0 and "view_changed" in rec.names()
    qtbot.waitUntil(lambda: s.state == "ready" and not s.view_busy and s.view_len() == 6, timeout=TIMEOUT)
    assert s.doc is not old_doc and s.doc.record_path == ("other",)
    # other records: the filter / sort of the old records are cleared (review finding A-F4:
    # they used to be re-applied to unrelated records, often giving an empty view)
    assert view_records(s) == [0, 1, 2, 3, 4, 5]
    assert s.filter is None and s.quick_filters == {} and s.sort_keys == [] and s.view_is_base
    qtbot.waitUntil(lambda: old_doc.closed, timeout=TIMEOUT)
    names = rec.names()
    assert "opened" in names and "index_finished" in names
    assert "view_changed" in names[names.index("opened"):]   # views see the new document
    # options not given are kept
    s.reopen()
    qtbot.waitUntil(lambda: s.state == "ready" and not s.view_busy, timeout=TIMEOUT)
    assert s.doc.record_path == ("other",) and s.record_path == "other"


def test_reopen_during_indexing(qtbot, make, tmp_path: Path, monkeypatch) -> None:
    slow_index(monkeypatch)
    s = make(big_file(tmp_path / "big.json", 100_000), use_cache=False)
    s.start()
    qtbot.waitUntil(lambda: s.state == "indexing" and s.doc.record_count > 0, timeout=TIMEOUT)
    old_doc = s.doc
    monkeypatch.undo()
    s.reopen()
    qtbot.waitUntil(lambda: s.state == "ready", timeout=TIMEOUT)
    assert s.view_len() == 100_000
    qtbot.waitUntil(lambda: old_doc.closed, timeout=TIMEOUT)


def test_close_is_idempotent_and_silent(qtbot, make, tmp_path: Path, monkeypatch, app) -> None:
    slow_index(monkeypatch)
    s = make(big_file(tmp_path / "big.json", 100_000), use_cache=False)
    s.start()
    qtbot.waitUntil(lambda: s.state == "indexing" and s.doc.record_count > 0, timeout=TIMEOUT)
    doc = s.doc
    got: list = []
    s.position_of(5, got.append)
    rec = Recorder(s)
    s.close()
    assert s.state == "closed" and s.closed and s.doc is None
    assert rec.events == [("state_changed", ("closed",))]
    s.close()
    assert rec.events == [("state_changed", ("closed",))]
    qtbot.wait(500)
    assert rec.events == [("state_changed", ("closed",))] and got == []
    qtbot.waitUntil(lambda: doc.closed, timeout=TIMEOUT)
    assert app.tasks.wait_idle(30)
    s.select(NodeRef(1, ()))
    s.reopen()
    s.set_filter(FilterSpec("i == 1"))
    s.set_sort([SortKey("i")])
    s.reset_view()
    s.back()
    hits = RecordSet.from_indices([1])
    s.show_records(hits, "x")
    assert hits.closed
    assert rec.events == [("state_changed", ("closed",))]


def test_close_during_filter_closes_record_sets(qtbot, make, tmp_path: Path, monkeypatch) -> None:
    slow_filter(monkeypatch, delay=0.02)
    s = open_ready(qtbot, make, big_file(tmp_path / "big.json", 100_000))
    views: list = []
    s.view_changed.connect(lambda: views.append(s.view_recordset()))
    s.set_filter(FilterSpec("g == 0"))
    qtbot.waitUntil(lambda: len(views) >= 1, timeout=TIMEOUT)
    s.close()
    assert views[0].closed
    assert s.app.tasks.wait_idle(30)


def test_app_context_reports_task_errors_and_settings(qtbot, app) -> None:
    def fail(ctx):
        raise ValueError("unhandled")

    app.tasks.run(fail, title="x")
    qtbot.waitUntil(lambda: len(app.errors) == 1, timeout=TIMEOUT)
    assert isinstance(app.errors[0], ValueError)
    from jsonreshape.errors import Cancelled
    app.report_error(Cancelled())                          # not an error
    assert len(app.errors) == 1
    from jsonreshape import settings as settings_mod
    new = settings_mod.Settings(sample_size=123)
    settings_mod.set_settings(new)
    app.settings_changed.emit()
    assert app.settings is new
    with qtbot.waitSignal(app.open_requested, timeout=TIMEOUT) as blocker:
        app.request_open("x.json", {"record_path": "data"})
    assert blocker.args == ["x.json", {"record_path": "data"}]
