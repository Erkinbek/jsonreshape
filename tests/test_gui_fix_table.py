"""Fix round FX3 of the table view (jsonreshape/gui/tableview.py) and its grid.

* P-F3: the record cache accounts the Python memory of what it keeps (not the raw bytes),
  keeps parsed values of small records only, and never parses a record larger than
  ``STREAM_RECORD_BYTES`` whole (Rust-extracted cells, equal to the parsed ones);
* A-F1: a reopen with another record path derives the default columns again;
* A-F2 / A-F3: copying one wildcard cell copies its values, a ``""`` cell copies ``""``;
* A-F7: a failed / cancelled schema computation never leaves the table stuck with ``$``;
* C1: the first activation follows the session's current node;
* C5: quick find (Ctrl+F, F3 / Shift+F3) over the loaded rows with highlights;
* C7: the data font follows the settings (zoom kept);
* K1 / K2: the filter row lives in the grid's public header strip; "clear all" is one
  pipeline run;
* C12: Uzbek wording "tartiblash".
"""

from __future__ import annotations

import json
import logging
import threading
import tracemalloc
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from jsonreshape import codec, core, i18n  # noqa: E402
from jsonreshape.gui import fields, tableview  # noqa: E402
from jsonreshape.gui.context import AppContext  # noqa: E402
from jsonreshape.gui.guistate import GuiState  # noqa: E402
from jsonreshape.gui.session import DocSession, NodeRef  # noqa: E402
from jsonreshape.gui.tableview import (  # noqa: E402
    KEEP_VALUE_BYTES,
    MAX_STREAM_CHARS,
    TableView,
    _row_copy_texts,
    cell_data,
    column_spec,
    copy_text,
    load_row,
    row_cost,
    value_cost,
)
from jsonreshape.gui.vgrid import VirtualGrid  # noqa: E402
from jsonreshape.reader import Document  # noqa: E402

pytestmark = pytest.mark.gui

TIMEOUT = 30000


# ----------------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------------
@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(10)
    ctx.errors = []
    ctx.error_reported.connect(ctx.errors.append)
    ctx.statuses = []
    ctx.status_message.connect(lambda text, _ms: ctx.statuses.append(text))
    yield ctx
    assert ctx.tasks.shutdown(30)


@pytest.fixture(autouse=True)
def no_swallowed_errors(caplog):
    caplog.set_level(logging.WARNING)
    yield
    records = caplog.get_records("setup") + caplog.get_records("call")
    bad = [r for r in records if r.name.startswith("jsonreshape.gui") and r.levelno >= logging.WARNING
           and ("grid source" in r.getMessage() or "RowLoader" in r.getMessage() or r.name.endswith("tableview"))]
    assert not bad, [r.getMessage() for r in bad]


@pytest.fixture
def opened(qtbot, app):
    made: list[DocSession] = []

    def open_file(path: Path, **kw) -> DocSession:
        kw.setdefault("use_cache", False)
        s = DocSession(app, path, **kw)
        made.append(s)
        with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
            s.start()
        return s

    yield open_file
    for s in made:
        s.close()
    assert app.tasks.wait_idle(30)


@pytest.fixture
def make_view(qtbot, opened):
    views: list[TableView] = []

    def make(session: DocSession, *, activate: bool = True, width: int = 1400) -> TableView:
        view = TableView(session)
        qtbot.addWidget(view)
        view.resize(width, 700)
        view.show()
        if activate:
            view.activate()
        views.append(view)
        return view

    yield make
    for v in views:
        v.shutdown()


def write_json(path: Path, value) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def write_jsonl(path: Path, recs: list) -> Path:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
    return path


def col_index(view: TableView, path: str) -> int:
    return next(i for i, c in enumerate(view.grid.columns()) if c.id == path)


def wait_text(qtbot, view: TableView, row: int, path: str, expected: str) -> None:
    qtbot.waitUntil(lambda: view.cell_text(row, path) == expected, timeout=TIMEOUT)


def wait_loaded(qtbot, view: TableView, rows, path: str) -> None:
    for row in rows:
        qtbot.waitUntil(lambda r=row: view.cell_text(r, path) is not None, timeout=TIMEOUT)


def clipboard() -> str:
    return QGuiApplication.clipboard().text()


def people(n: int) -> list[dict]:
    return [{"id": i, "name": f"user{i}", "city": ["Toshkent", "Samarqand", "Buxoro"][i % 3],
             "tags": ["a", "b", "c"][: i % 4], "orders": [{"sum": k + 0.5} for k in range(i % 3)]}
            for i in range(n)]


# ----------------------------------------------------------------------------------
# P-F3: memory accounting, kept values, records never parsed whole
# ----------------------------------------------------------------------------------
def _measured(build) -> int:
    """Bytes still allocated after ``build()`` (its result kept alive)."""
    tracemalloc.start()
    try:
        before = tracemalloc.take_snapshot()
        value = build()
        after = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()
    total = sum(stat.size_diff for stat in after.compare_to(before, "filename"))
    del value
    return total


@pytest.mark.parametrize("shape", ["objects", "numbers", "strings", "nested"])
def test_value_cost_is_close_to_the_real_python_memory(shape) -> None:
    """The cache accounts parsed values by their Python objects (the old estimate -- the raw
    JSON size -- was 5-15 times too small: 64 MiB "accounted" were ~1 GB)."""
    if shape == "objects":
        value = [{"a": k, "b": k % 7, "name": f"n{k}"} for k in range(3000)]
    elif shape == "numbers":
        value = [k * 1000003 for k in range(20000)]
    elif shape == "strings":
        value = {f"key{k}": "text " * (k % 20) for k in range(3000)}
    else:
        value = {"a": [{"b": [{"c": [k, str(k), None, True]}]} for k in range(1500)]}
    raw = codec.dumps(value)
    real = _measured(lambda: codec.loads(raw))
    est = value_cost(codec.loads(raw))
    assert real > 2 * len(raw)                     # the raw size says little about the memory
    assert 0.8 * real <= est <= 3 * real, (shape, len(raw), real, est)


def test_only_small_records_keep_their_parsed_value(tmp_path) -> None:
    small = {"id": 1, "s": "x" * 1000}
    medium = {"id": 2, "vals": [{"a": k, "b": k % 7} for k in range(3000)]}      # ~40 KB
    path = write_jsonl(tmp_path / "m.jsonl", [small, medium])
    specs = tuple(column_spec(c) for c in ("id", "vals", "s"))
    with Document.open(path, use_cache=False) as doc:
        doc.build_index()
        rows = [load_row(doc, i, specs, 0) for i in range(2)]
        assert doc.record_span(1)[1] - doc.record_span(1)[0] > KEEP_VALUE_BYTES
    assert rows[0].value == small and not rows[0].streamed
    assert rows[1].value is tableview._NOVALUE and not rows[1].streamed     # parsed, only cells kept
    assert rows[1].cells["vals"].cell.text == "[3000]" and rows[1].cells["id"].full == "2"
    # the accounted size follows what is kept: the medium row costs a few hundred bytes
    assert row_cost(rows[1]) < 3000
    assert row_cost(rows[0]) >= value_cost(small) > len(json.dumps(small))


def test_cache_budget_holds_in_real_memory(qtbot, app, opened, make_view, tmp_path, monkeypatch) -> None:
    """The loader evicts by the estimated Python memory: with a 400 KB budget and records
    whose parsed values take ~100 KB each, only a few rows stay cached; lazily computed
    cells of a new column are accounted too."""
    monkeypatch.setattr(tableview, "RECORD_CACHE_BYTES", 400_000)
    recs = [{"id": i, "v": [{"a": k, "s": f"s{k}"} for k in range(600)]} for i in range(400)]   # ~15 KB each
    path = write_jsonl(tmp_path / "budget.jsonl", recs)
    s = opened(path)
    view = make_view(s, width=900)
    view.set_columns(["id", "v"])
    loader = view._loader
    for top in range(0, 400, 100):
        view.grid.scroll_to(top, "top")
        wait_text(qtbot, view, top, "id", str(top))
    st = loader._state
    rows = [v for v in st.cache.values() if isinstance(v, tableview._Row)]
    assert rows and all(r.value is not tableview._NOVALUE for r in rows)       # small records: kept
    assert loader.cached_bytes() == sum(st.sizes.values())
    # rows on screen (the loader's window) are never evicted for their size; everything else
    # stays within the budget
    window = [k for k in st.cache if k in st.window]
    assert loader.cached_bytes() <= 400_000 + (len(window) + 1) * max(st.sizes.values())
    assert len(rows) <= len(window) + 400_000 // min(st.sizes.values()) + 1
    assert len(rows) < 200                                      # the raw-byte estimate kept all 400
    assert all(st.sizes[r.record] >= value_cost(r.value) for r in rows)
    before = loader.cached_bytes()
    view.add_column("v[*].s")                                   # computed on the GUI thread from kept values
    row = view.grid.top_row()
    qtbot.waitUntil(lambda: view.cell_text(row, "v[*].s") is not None, timeout=TIMEOUT)
    assert loader.cached_bytes() != before or len(st.cache) < len(rows)
    window = [k for k in st.cache if k in st.window]
    assert loader.cached_bytes() <= 400_000 + (len(window) + 1) * max(st.sizes.values())


def _records_for_extraction() -> list:
    long = "ü" * (MAX_STREAM_CHARS + 10)
    return [
        {"id": 0, "name": "plain", "arr": list(range(700)), "obj": {"a": 1, "b": {"c": [1, 2]}},
         "w": [{"x": 1}, {"x": "t"}, {"y": 3}, {"x": {"deep": True}}], "n": None, "f": 1.10, "big": 12345678901234567890},
        {"id": 1, "name": "long string", "s": long, "pad": "p" * 9000},
        {"id": 2, "w": [{"x": k} for k in range(1500)], "e": "", "z": [], "o": {}},
        {"id": 3, "pad": "q" * 9000, "dup": 1, "nested": {"dup": [1, 2, 3]}},
    ]


def _write_dup_record(path: Path, recs: list) -> Path:
    body = ",".join(json.dumps(r, ensure_ascii=False) for r in recs)
    dup = '{"id":4,"dup":"first","pad":"' + "r" * 9000 + '","dup":["last",2],"name":"d"}'
    path.write_text("[" + body + "," + dup + "]", encoding="utf-8")
    return path


COLUMNS = ["id", "name", "arr", "obj", "obj.b", "obj.b.c", "w[*].x", "w[*]", "n", "f", "big", "s", "pad", "e",
           "z", "o", "dup", "nested.dup[*]", "missing", "$", "**.x", "w[1].x", "*"]


@pytest.mark.skipif(core.BACKEND != "rust", reason="the fast path needs the Rust core")
def test_extracted_cells_equal_the_parsed_ones(tmp_path, monkeypatch) -> None:
    """Records above STREAM_RECORD_BYTES are never parsed whole: their cells come from the
    Rust extractor (scalars one by one, containers counted) and equal the cells of the
    parsed record (containers show their counts, long strings are cut)."""
    monkeypatch.setattr(tableview, "STREAM_RECORD_BYTES", 2048)
    path = _write_dup_record(tmp_path / "x.json", _records_for_extraction())
    specs = tuple(column_spec(c) for c in COLUMNS)
    big_loads: list[int] = []
    orig = codec.loads

    def spy(data, *a, **k):
        if isinstance(data, (bytes, bytearray, memoryview)) and len(data) > 2048 and \
                not bytes(data[:1]) == b'"':
            big_loads.append(len(data))
        return orig(data, *a, **k)

    with Document.open(path, use_cache=False) as doc:
        doc.build_index()
        n = doc.record_count
        parsed = [orig(doc.record_bytes(i)) for i in range(n)]
        monkeypatch.setattr(codec, "loads", spy)
        rows = [load_row(doc, i, specs, 0) for i in range(n)]
        copies = [_row_copy_texts(doc, i, list(specs), None, None) for i in range(n)]
        monkeypatch.setattr(codec, "loads", orig)
    assert big_loads == []                                       # no record (container) was parsed whole
    for i, (row, value) in enumerate(zip(rows, parsed)):
        assert row.streamed and not row.huge and row.value is tableview._NOVALUE
        for spec in specs:
            want = cell_data(value, spec, i)
            got = row.cells[spec.text]
            assert got.count == want.count, (i, spec.text)
            if want.count == 0:
                assert got.cell.text == "", (i, spec.text)
                continue
            assert got.cell.role == want.cell.role, (i, spec.text)
            cut = i == 1 and spec.text in ("s", "*")          # the long string is cut (and so not exact)
            if spec.text == "s" and i == 1:
                assert got.full == value["s"][:MAX_STREAM_CHARS] + "…"
            else:
                assert got.cell.text == want.cell.text, (i, spec.text, got.cell.text, want.cell.text)
            if cut:
                assert not got.exact
            elif want.exact:
                assert got.exact and got.full == want.full, (i, spec.text)
        # TSV copy texts of a streamed (not huge) record are complete (compact JSON, no "[n]")
        assert copies[i] == [copy_text(value, spec, i) for spec in specs], i
    assert rows[4].cells["dup"].cell.text == '[2]' and rows[4].cells["name"].full == "d"


@pytest.mark.skipif(core.BACKEND != "rust", reason="the fast path needs the Rust core")
def test_member_records_are_extracted_too(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(tableview, "STREAM_RECORD_BYTES", 1024)
    value = {f"k{i}": {"id": i, "pad": "z" * 3000, "w": [{"x": i}, {"x": -i}]} for i in range(4)}
    path = write_json(tmp_path / "keyed.json", value)
    specs = tuple(column_spec(c) for c in ("$key", "$index", "id", "w[*].x", "$"))
    with Document.open(path, use_cache=False) as doc:
        doc.build_index()
        assert doc.member_records
        rows = [load_row(doc, i, specs, 0) for i in range(doc.record_count)]
    for i, row in enumerate(rows):
        assert row.streamed and row.key == f"k{i}"
        assert [row.cells[s.text].cell.text for s in specs] == [f"k{i}", str(i), str(i), f"{i}; {-i}", "{3}"]


def test_huge_single_document_is_not_parsed_on_any_thread(qtbot, app, opened, make_view, tmp_path,
                                                          monkeypatch) -> None:
    """A single document above STREAM_RECORD_BYTES: the table's loader shows its counts
    without ``codec.loads`` of the whole value (that held the GIL ~0.9 s and took 1.2 GB
    for 45 MB). (The schema sample is :mod:`jsonreshape.schema`'s business.)"""
    monkeypatch.setattr(tableview, "STREAM_RECORD_BYTES", 4096)
    value = {"a": [{"k": i % 10} for i in range(3000)], "b": [{"k": i} for i in range(2000)], "t": "x"}
    path = write_json(tmp_path / "single.json", value)
    s = opened(path, format="single")
    sizes: list[int] = []
    orig = codec.loads

    def spy(data, *a, **k):
        if isinstance(data, (bytes, bytearray, memoryview)) and threading.current_thread().name.startswith("RowLoader"):
            sizes.append(len(data))
        return orig(data, *a, **k)

    monkeypatch.setattr(codec, "loads", spy)
    view = make_view(s)
    view.set_columns(["a", "b", "t", "a[*].k"])
    wait_text(qtbot, view, 0, "a", "[3000]")
    assert view.cell_text(0, "b") == "[2000]" and view.cell_text(0, "t") == "x"
    assert view.cell_text(0, "a[*].k").startswith("0; 1; 2")
    # the Rust extractor decodes the matched values one by one (small loads); the pure-Python
    # core streams the record instead and never calls codec.loads at all
    assert all(size <= 4096 for size in sizes), max(sizes, default=0)
    if core.BACKEND == "rust":
        assert sizes


# ----------------------------------------------------------------------------------
# A-F1: reopen with another record path
# ----------------------------------------------------------------------------------
def test_reopen_with_another_record_path_derives_new_default_columns(qtbot, app, opened, make_view,
                                                                     tmp_path) -> None:
    path = write_json(tmp_path / "two.json", {"a": [{"x": i, "xx": i} for i in range(5)],
                                             "b": [{"y": i, "yy": -i} for i in range(7)]})
    s = opened(path, record_path="a")
    view = make_view(s)
    qtbot.waitUntil(lambda: view._columns_source == "default", timeout=TIMEOUT)
    assert view.column_paths() == ["x", "xx"]
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen(record_path="b")
    qtbot.waitUntil(lambda: view.column_paths() == ["y", "yy"], timeout=TIMEOUT)
    wait_text(qtbot, view, 6, "yy", "-6")
    # F5 (same options) keeps the columns as they are
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen()
    qtbot.wait(200)
    assert view.column_paths() == ["y", "yy"] and view._columns_source == "default"
    # the user's columns survive another record path
    view.set_columns(["yy", "$index"])
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen(record_path="a")
    qtbot.wait(300)
    assert view.column_paths() == ["yy", "$index"] and view._columns_source == "user"
    wait_text(qtbot, view, 4, "$index", "4")


def test_reopen_before_activation_uses_the_new_records(qtbot, app, opened, make_view, tmp_path) -> None:
    path = write_json(tmp_path / "two.json", {"a": [{"x": i} for i in range(3)], "b": [{"y": i} for i in range(4)]})
    s = opened(path, record_path="a")
    view = make_view(s)
    qtbot.waitUntil(lambda: view.column_paths() == ["x"], timeout=TIMEOUT)
    view.hide()
    seen: list[list[str]] = []
    view.columns_changed.connect(lambda: seen.append(view.column_paths()))
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen(record_path="b")
    assert ["x"] not in seen and view.column_paths() in (["$"], ["y"])   # never the old fields with empty cells
    view.show()
    view.activate()
    qtbot.waitUntil(lambda: view.column_paths() == ["y"], timeout=TIMEOUT)


# ----------------------------------------------------------------------------------
# A-F2 / A-F3: copying one cell
# ----------------------------------------------------------------------------------
def _copy(qtbot, app, view: TableView, row: int, path: str, fmt: str = "json") -> tuple[str, list[str]]:
    QGuiApplication.clipboard().setText("<untouched>")
    app.statuses.clear()
    view.grid.set_current(row, col_index(view, path))
    result = view.copy_value(fmt)
    if result is not None and not isinstance(result, str):
        qtbot.waitUntil(lambda: result.state != "running", timeout=TIMEOUT)
    qtbot.wait(30)
    return clipboard(), list(app.statuses)


def test_single_cell_copy_of_wildcards_and_empty_strings(qtbot, app, opened, make_view, tmp_path, lang_en) -> None:
    small = {"s": "", "z": 0, "f": False, "n": None, "arr": [{"x": 1}, {"x": 2}], "one": [{"x": "only"}],
             "t": "hello", "deep": {"l": [[1, 2], [3]]}}
    big = dict(small, pad="p" * (KEEP_VALUE_BYTES + 100))          # its parsed value is not kept
    path = write_jsonl(tmp_path / "c.jsonl", [small, big])
    s = opened(path)
    view = make_view(s)
    cols = ["s", "z", "f", "n", "arr[*].x", "one[*].x", "t", "missing", "deep.l[*][*]", "arr[*].nope"]
    view.set_columns(cols)
    for row in (0, 1):
        wait_loaded(qtbot, view, [row], "t")
    assert view._row(1).value is tableview._NOVALUE and view._row(0).value is not tableview._NOVALUE
    no_value = i18n.t("gui.table.copy.no_value")
    for row in (0, 1):
        assert _copy(qtbot, app, view, row, "s")[0] == '""'                       # A-F3
        assert _copy(qtbot, app, view, row, "z")[0] == "0"
        assert _copy(qtbot, app, view, row, "arr[*].x")[0] == "[\n  1,\n  2\n]"   # A-F2: the values, not arr
        assert _copy(qtbot, app, view, row, "arr[*].x", "json_compact")[0] == "[1,2]"
        assert _copy(qtbot, app, view, row, "arr[*].x", "python")[0].replace("\n", "").replace(" ", "") == "[1,2]"
        assert _copy(qtbot, app, view, row, "one[*].x")[0] == '"only"'
        assert _copy(qtbot, app, view, row, "deep.l[*][*]", "json_compact")[0] == "[1,2,3]"
        text, statuses = _copy(qtbot, app, view, row, "missing")
        assert text == "<untouched>" and statuses == [no_value]
        text, statuses = _copy(qtbot, app, view, row, "arr[*].nope")
        assert text == "<untouched>" and statuses == [no_value]
    assert app.errors == []


@pytest.mark.skipif(core.BACKEND != "rust", reason="the fast path needs the Rust core")
def test_single_cell_copy_of_a_streamed_wildcard_cell(qtbot, app, opened, make_view, tmp_path,
                                                       monkeypatch) -> None:
    """A streamed row knows only where a wildcard column's values lie: one match must
    still copy that value, not the node the wildcard starts in."""
    monkeypatch.setattr(tableview, "STREAM_RECORD_BYTES", 2048)
    rec = {"w": [{"x": 7}, {"y": 1}], "pad": "q" * 5000, "v": [{"x": 1}, {"x": 2}]}
    path = write_jsonl(tmp_path / "w.jsonl", [rec])
    s = opened(path)
    view = make_view(s)
    view.set_columns(["w[*].x", "v[*].x", "pad"])
    wait_text(qtbot, view, 0, "w[*].x", "7")
    assert view._row(0).streamed
    assert _copy(qtbot, app, view, 0, "w[*].x")[0] == "7"
    assert _copy(qtbot, app, view, 0, "v[*].x", "json_compact")[0] == "[1,2]"


# ----------------------------------------------------------------------------------
# A-F7: schema computation failed / cancelled
# ----------------------------------------------------------------------------------
def test_cancelled_schema_is_asked_again_on_activation(qtbot, app, opened, make_view, tmp_path, monkeypatch,
                                                       lang_en) -> None:
    path = write_json(tmp_path / "a.json", [{"x": i, "y": str(i)} for i in range(50)])
    s = opened(path)
    gate = threading.Event()
    real = fields.infer_schema

    def slow(*a, **kw):
        cancel = kw.get("cancel")
        while not gate.is_set():
            if cancel is not None:
                cancel.check()
            gate.wait(0.02)
        return real(*a, **kw)

    monkeypatch.setattr(fields, "infer_schema", slow)
    view = make_view(s)
    assert "Taking the columns" in view.grid.columns()[0].tooltip
    qtbot.waitUntil(lambda: any(t.kind == "schema" for t in app.tasks.tasks(owner=s)), timeout=TIMEOUT)
    for task in app.tasks.tasks(owner=s):
        if task.kind == "schema":
            task.cancel()
    qtbot.waitUntil(lambda: not view._schema_wanted, timeout=TIMEOUT)      # told about the cancellation
    assert view.column_paths() == ["$"] and "Taking the columns" not in view.grid.columns()[0].tooltip
    gate.set()
    view.activate()                                                         # the next activation retries
    qtbot.waitUntil(lambda: view.column_paths() == ["x", "y"], timeout=TIMEOUT)


def test_failed_schema_leaves_a_usable_table(qtbot, app, opened, make_view, tmp_path, monkeypatch, lang_en) -> None:
    path = write_json(tmp_path / "a.json", [{"x": i} for i in range(5)])
    s = opened(path)
    calls = {"n": 0}
    real = fields.infer_schema

    def broken(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk gone")
        return real(*a, **kw)

    monkeypatch.setattr(fields, "infer_schema", broken)
    view = make_view(s)
    qtbot.waitUntil(lambda: view._schema_failed, timeout=TIMEOUT)
    assert app.errors                                                       # reported once by fields
    wait_text(qtbot, view, 0, "$", "{1}")                                   # the $ column works meanwhile
    view.activate()
    qtbot.waitUntil(lambda: view.column_paths() == ["x"], timeout=TIMEOUT)
    assert not view._schema_failed


# ----------------------------------------------------------------------------------
# C1: first activation follows the session
# ----------------------------------------------------------------------------------
def test_first_activation_selects_the_current_node(qtbot, app, opened, make_view, tmp_path) -> None:
    path = write_json(tmp_path / "p.json", people(300))
    s = opened(path)
    s.select(NodeRef(150, ("name",)), origin=object())               # e.g. the tree, before the table exists
    view = make_view(s, activate=False)
    assert view.grid.current() == (-1, -1)
    view.activate()
    qtbot.waitUntil(lambda: view.grid.current()[0] == 150, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: view._columns_source == "default", timeout=TIMEOUT)
    qtbot.waitUntil(lambda: view.grid.columns()[view.grid.current()[1]].id == "name", timeout=TIMEOUT)
    first, last = view.grid.visible_rows()
    assert first <= 150 <= last
    assert s.current == NodeRef(150, ("name",))                      # following did not select anything else
    # a later activation does not jump back
    view.grid.set_current(3, 0)
    view.activate()
    qtbot.wait(100)
    assert view.grid.current()[0] == 3


# ----------------------------------------------------------------------------------
# C5: quick find
# ----------------------------------------------------------------------------------
def test_quick_find_highlights_and_steps_through_loaded_cells(qtbot, app, opened, make_view, tmp_path,
                                                             lang_en) -> None:
    path = write_json(tmp_path / "p.json", people(40))
    s = opened(path)
    view = make_view(s)
    view.set_columns(["id", "name", "city"])
    wait_loaded(qtbot, view, range(20), "city")
    view.grid.set_current(0, 0)
    assert not view.find_bar.isVisible()
    view.quick_find()                                                 # Ctrl+F
    assert view.find_bar.isVisible()
    view.find_bar._edit.setText("samar")
    view.find_bar.flush()
    qtbot.waitUntil(lambda: bool(view.quick_find_matches()), timeout=TIMEOUT)
    matches = view.quick_find_matches()
    loaded = [r for r in range(40) if view.cell_text(r, "city") is not None]
    assert set(matches) == {(r, "city") for r in loaded if r % 3 == 1}
    assert all(spans == ((0, 5),) for spans in matches.values())
    # the first hit after the current cell is current and highlighted as such
    assert view.grid.current() == (1, col_index(view, "city"))
    cell = view.cell_at(1, "city")
    assert cell.spans == ((0, 5, True),) and view.cell_at(4, "city").spans == ((0, 5),)
    assert view.cell_at(0, "city").spans == ()
    status = view.find_bar.status_text()
    assert status.startswith("1 of ")
    view.find_next()                                                  # F3
    assert view.grid.current()[0] == 4
    view.find_next(backwards=True)                                    # Shift+F3
    assert view.grid.current()[0] == 1
    view.find_next(backwards=True)                                    # wraps to the last hit
    assert view.grid.current()[0] == max(r for r, _c in matches)
    # matching is case-insensitive and apostrophe-folding, numbers by their text
    view.find_bar._edit.setText("1")
    view.find_bar.flush()
    qtbot.waitUntil(lambda: (1, "id") in view.quick_find_matches(), timeout=TIMEOUT)
    assert (1, "name") in view.quick_find_matches()                    # "user1"
    # nothing found
    view.find_bar._edit.setText("no such text")
    view.find_bar.flush()
    qtbot.waitUntil(lambda: view.find_bar.status_text() == i18n.t("gui.quickfind.none"), timeout=TIMEOUT)
    assert view.quick_find_matches() == {}
    view.find_bar.close_bar()
    assert view.quick_find_matches() == {} and view.cell_at(1, "city").spans == ()


def test_quick_find_follows_the_view_and_new_rows(qtbot, app, opened, make_view, tmp_path) -> None:
    path = write_json(tmp_path / "p.json", people(400))
    s = opened(path)
    view = make_view(s, width=900)
    view.set_columns(["id", "name"])
    wait_loaded(qtbot, view, range(5), "name")
    view.quick_find()
    view.find_bar._edit.setText("user39")
    view.find_bar.flush()
    qtbot.waitUntil(lambda: (39, "name") in view.quick_find_matches(), timeout=TIMEOUT)
    assert (390, "name") not in view.quick_find_matches()              # not loaded yet
    assert "+" in view.find_bar.status_text()                          # more rows than were searched
    view.grid.scroll_to(390, "top")
    qtbot.waitUntil(lambda: (390, "name") in view.quick_find_matches(), timeout=TIMEOUT)   # rescanned
    # a filter moves the rows: the hits follow the new positions (rows whose records are
    # cached already join the search when they are scrolled into view)
    view.apply_quick_filter("id", ">=300")
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: (39, "name") not in view.quick_find_matches(), timeout=TIMEOUT)
    view.grid.scroll_to(90, "top")
    qtbot.waitUntil(lambda: (90, "name") in view.quick_find_matches(), timeout=TIMEOUT)
    assert view.record_at_row(90) == 390 and (39, "name") not in view.quick_find_matches()
    # a column change rescans too
    view.set_columns(["name"])
    qtbot.waitUntil(lambda: (90, "name") in view.quick_find_matches(), timeout=TIMEOUT)


def test_find_keys_without_the_main_window_and_alt_down(qtbot, app, opened, make_view, tmp_path) -> None:
    path = write_json(tmp_path / "p.json", people(10))
    s = opened(path)
    view = make_view(s)
    view.set_columns(["id", "name"])
    wait_loaded(qtbot, view, range(3), "name")
    view.activateWindow()
    view.grid.set_current(0, 1)
    view.grid.setFocus()
    qtbot.waitUntil(view.grid.hasFocus, timeout=3000)
    qtbot.keyClick(view.grid, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert view.find_bar.isVisible()
    qtbot.keyClicks(view.find_bar._edit, "user2")
    qtbot.keyClick(view.find_bar._edit, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: view.grid.current()[0] == 2, timeout=TIMEOUT)
    qtbot.keyClick(view.find_bar._edit, Qt.Key.Key_Escape)
    assert not view.find_bar.isVisible()
    view.grid.setFocus()
    qtbot.waitUntil(view.grid.hasFocus, timeout=3000)
    qtbot.keyClick(view.grid, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)     # the column's filter
    qtbot.waitUntil(lambda: view.filter_editor("name").hasFocus(), timeout=3000)
    assert view.filter_button.isVisible() and view.filter_row_visible()


# ----------------------------------------------------------------------------------
# K1 / K2
# ----------------------------------------------------------------------------------
def test_filter_row_uses_the_public_header_strip(qtbot, opened, make_view, tmp_path) -> None:
    s = opened(write_json(tmp_path / "p.json", people(5)))
    view = make_view(s)
    assert type(view.grid) is VirtualGrid                              # no private layout overrides
    assert view.grid.header_extra() is view.strip
    view.set_filter_row_visible(False)
    assert view.grid.header_extra_height() == 0 and not view.layout_state()["filter_row"]
    view.set_filter_row_visible(True)
    assert view.grid.header_extra_height() == view.strip.preferred_height() > 0
    from PySide6.QtWidgets import QLineEdit

    font = view.font()
    font.setPointSizeF(font.pointSizeF() + 4)
    view.setFont(font)                                               # FontChange: the strip is measured again
    assert view.grid.header_extra_height() == view.strip.preferred_height()
    assert set(view.strip.findChildren(QLineEdit)) == set(view.strip.editors.values())   # no stray probe


def test_clear_all_quick_filters_is_one_pipeline_run(qtbot, app, opened, make_view, tmp_path) -> None:
    s = opened(write_json(tmp_path / "p.json", people(60)))
    view = make_view(s)
    view.set_columns(["id", "name", "city"])
    for path, text in (("id", "<50"), ("city", "toshkent"), ("name", "user")):
        view.apply_quick_filter(path, text)
        qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert len(s.quick_filters) == 3
    calls: list = []
    real = s.set_quick_filters

    def spy(mapping):
        calls.append(dict(mapping))
        return real(mapping)

    s.set_quick_filters = spy
    single: list = []
    s.set_quick_filter = lambda *a, **k: single.append(a)
    changes: list = []
    s.view_changed.connect(lambda: changes.append(1))
    try:
        view.clear_quick_filters()
        qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    finally:
        del s.set_quick_filters
        del s.set_quick_filter
    assert calls == [{}] and single == [] and s.quick_filters == {}
    assert len(changes) <= 2 and s.view_len() == 60
    assert all(not view.filter_editor(p).text() for p in ("id", "name", "city"))


# ----------------------------------------------------------------------------------
# C7: the data font follows the settings
# ----------------------------------------------------------------------------------
def test_data_font_follows_settings_and_keeps_zoom(qtbot, app, opened, make_view, tmp_path) -> None:
    s = opened(write_json(tmp_path / "p.json", people(5)))
    view = make_view(s)
    base = view.grid.base_font().pointSizeF()
    view.zoom(2)
    assert view.grid.zoom_level() == 2
    app.state.set("mono_font", "Courier New")
    app.state.set("mono_size", 15)
    app.settings_changed.emit()
    assert view.grid.base_font().pointSizeF() == 15 != base
    assert view.grid.zoom_level() == 2 and view.grid.grid_font().pointSizeF() == 17
    view.shutdown()
    app.state.set("mono_size", 9)
    app.settings_changed.emit()                                        # disconnected: nothing happens
    assert view.grid.base_font().pointSizeF() == 15


# ----------------------------------------------------------------------------------
# C12
# ----------------------------------------------------------------------------------
def test_table_and_grid_say_tartiblash() -> None:
    texts = [v["uz"] for k, v in i18n.MESSAGES.items() if k.startswith(("gui.table.", "gui.grid."))]
    assert texts and not [x for x in texts if "saral" in x.lower()]
    assert "tartiblash" in i18n.MESSAGES["gui.table.menu.sort_asc"]["uz"]


def test_copy_of_a_large_record_over_the_capture_budget(tmp_path, monkeypatch) -> None:
    """A value too big for the capture budget copies as its size text (like a huge record),
    smaller ones of the same record completely."""
    monkeypatch.setattr(tableview, "STREAM_RECORD_BYTES", 1024)
    rec = {"id": 7, "big": [{"k": i} for i in range(3000)], "small": [1, 2, 3], "s": "t"}
    path = write_jsonl(tmp_path / "b.jsonl", [rec])
    specs = [column_spec(c) for c in ("id", "small", "s", "big")]
    with Document.open(path, use_cache=False) as doc:
        doc.build_index()
        assert _row_copy_texts(doc, 0, specs, None, None) == ["7", "[1,2,3]", "t", codec.dumps(rec["big"]).decode()]
        monkeypatch.setattr(tableview, "capture_budget", lambda settings: 20_000)
        assert _row_copy_texts(doc, 0, specs, None, None) == ["7", "[1,2,3]", "t", "[3000]"]


def test_reopen_with_another_record_path_empties_the_quick_filter_editors(qtbot, app, opened, make_view,
                                                                         tmp_path) -> None:
    """The session drops filter / quick filters / sort when the record path changes (A-F4):
    the editors follow -- applied, invalid and still pending texts alike."""
    path = write_json(tmp_path / "two.json", {"a": [{"x": i, "y": i} for i in range(9)],
                                             "b": [{"x": -i, "y": i} for i in range(7)]})
    s = opened(path, record_path="a")
    view = make_view(s)
    view.set_columns(["x", "y"])                                   # user columns: kept by the reopen
    assert view.apply_quick_filter("x", ">=3")
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert s.view_len() == 6
    assert not view.apply_quick_filter("y", ">abc")                # invalid: stays in its editor
    assert view.filter_editor("y").error
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen(record_path="b")
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert s.quick_filters == {} and s.view_len() == 7
    for p in ("x", "y"):
        ed = view.filter_editor(p)
        assert ed.text() == "" and not ed.error and not ed.pending(), p
    assert not view.clear_button.isEnabled()
    wait_text(qtbot, view, 6, "x", "-6")
