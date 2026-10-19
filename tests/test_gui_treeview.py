"""TreeView (jsonreshape/gui/treeview.py + treemodel.py; ARCHITECTURE §11.7 "Tree", AGENT_PROMPT §4.1).

Real sessions on generated files in every format (array, JSON Lines, keyed, wrapped with
meta + data, single object, brace list): top rows and labels, expanding / collapsing
records and nested containers, expansion kept across filter / sort / reset, live growth
while indexing, large nodes through the children task, a 1 M-element array (timing),
expand all (confirm / cap), expand to level, collapse all, selection -> session.current
(path / ipath per format), reveal from key-only refs, a record outside the view, the
breadcrumb, keyboard, node sorting (Uzbek key order, array elements by a field), quick
find, the context menu, the full value window and no Node calls on the GUI thread.
Pure helpers of treemodel are tested without Qt at the end.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from jsonreshape import reader as reader_mod  # noqa: E402
from jsonreshape.gui import widgets  # noqa: E402
from jsonreshape.gui.context import AppContext  # noqa: E402
from jsonreshape.gui.guistate import GuiState  # noqa: E402
from jsonreshape.gui.session import DocSession, FilterSpec, NodeRef  # noqa: E402
from jsonreshape.gui.treemodel import ROOT, WRAPPER  # noqa: E402
from jsonreshape.gui.treeview import EXPAND_CAP_ROWS, ElementSortDialog, TreeView  # noqa: E402
from jsonreshape.reader import Document, Node  # noqa: E402
from jsonreshape.sortkeys import SortKey  # noqa: E402

pytestmark = pytest.mark.gui

TIMEOUT = 30000


# ----------------------------------------------------------------------------------
# fixtures / helpers
# ----------------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_modal_confirm(monkeypatch):
    """No test may block on a message box: confirmations answer "yes" unless a test says
    otherwise."""
    monkeypatch.setattr(widgets, "confirm_handler", lambda parent, text, title: True)


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


@pytest.fixture
def opened(qtbot, app):
    made: list = []

    def open_file(path: Path, *, wait: bool = True, **kw) -> DocSession:
        kw.setdefault("use_cache", False)
        s = DocSession(app, path, **kw)
        made.append(s)
        if wait:
            with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
                s.start()
        else:
            with qtbot.waitSignal(s.opened, timeout=TIMEOUT):
                s.start()
        return s

    yield open_file
    for s in made:
        s.close()
    assert app.tasks.wait_idle(30)


@pytest.fixture
def views(qtbot):
    made: list[TreeView] = []

    def make(session: DocSession, *, activate: bool = True) -> TreeView:
        view = TreeView(session)
        qtbot.addWidget(view)
        view.resize(1000, 600)
        view.show()
        made.append(view)
        if activate:
            view.activate()
        return view

    yield make
    for v in made:
        v.shutdown()


def write(path: Path, value) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def people(n: int) -> list[dict]:
    return [{"id": i, "name": f"user{i}", "city": ["Toshkent", "Samarqand", "Buxoro"][i % 3],
             "tags": ["a", "b", "c"][: i % 4], "contact": {"phone": f"+998{i:07d}", "zip": i * 10}}
            for i in range(n)]


def wait_text(qtbot, view: TreeView, row: int, col: str, text: str | None = None) -> str:
    """Wait until the cell is loaded (and equals ``text`` when given)."""
    def ok() -> bool:
        if row >= view.row_count():
            return False
        got = view.cell_text(row, col)
        return got is not None and (text is None or got == text)

    qtbot.waitUntil(ok, timeout=TIMEOUT)
    return view.cell_text(row, col)


def wait_idle(qtbot, view: TreeView) -> None:
    qtbot.waitUntil(lambda: not view.busy(), timeout=TIMEOUT)


def labels(qtbot, view: TreeView, first: int = 0, last: int | None = None) -> list[str]:
    last = view.row_count() if last is None else min(last, view.row_count())
    return [wait_text(qtbot, view, r, "key") for r in range(first, last)]


def expand(qtbot, view: TreeView, row: int) -> None:
    kp = view.row_keypath(row)
    wait_text(qtbot, view, row, "key")
    view.expand_node(kp)
    qtbot.waitUntil(lambda: view.flat.is_expanded(kp) and not view.is_loading(kp), timeout=TIMEOUT)


def row_by_label(qtbot, view: TreeView, label: str, start: int = 0) -> int:
    for r in range(start, view.row_count()):
        if wait_text(qtbot, view, r, "key") == label:
            return r
    raise AssertionError(f"no row {label!r}")


# ----------------------------------------------------------------------------------
# formats: top rows, labels, expand / collapse
# ----------------------------------------------------------------------------------
def test_array_rows_labels_and_columns(qtbot, opened, views, tmp_path: Path, lang_uz) -> None:
    s = opened(write(tmp_path / "people.json", people(50)))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 50, timeout=TIMEOUT)
    assert labels(qtbot, view, 0, 3) == ["#0", "#1", "#2"]
    assert wait_text(qtbot, view, 0, "value").startswith('{5} id: 0, name: "user0", city: "Toshkent"')
    assert view.cell_text(0, "children") == "5"
    cell = view.cell_at(0, "key")
    assert cell.badge == "object" and cell.expander == 1 and cell.depth == 0
    size = view.cell_at(0, "size")
    assert size.align == "right" and "bayt" in size.tooltip and "–" in size.tooltip
    # expand record #1: members in file order, keys colored as keys, values typed
    expand(qtbot, view, 1)
    assert view.row_count() == 55
    assert labels(qtbot, view, 2, 7) == ["id", "name", "city", "tags", "contact"]
    assert view.cell_at(2, "key").depth == 1 and view.cell_at(2, "key").role == "key"
    assert wait_text(qtbot, view, 3, "value") == '"user1"' and view.cell_at(3, "value").role == "string"
    assert view.cell_at(2, "value").role == "number"
    assert wait_text(qtbot, view, 5, "value") == "[1] \"a\""
    assert view.cell_at(1, "key").expander == 2
    # nested container: tags[0] shows its file index
    expand(qtbot, view, 5)
    assert wait_text(qtbot, view, 6, "key") == "[0]" and view.cell_at(6, "key").depth == 2
    expand(qtbot, view, 7)   # contact
    assert labels(qtbot, view, 8, 10) == ["phone", "zip"]
    # collapse keeps the inner expansion; re-expanding restores it
    assert view.collapse_row(1)
    assert view.row_count() == 50
    expand(qtbot, view, 1)
    assert view.row_count() == 55 + 1 + 2
    assert wait_text(qtbot, view, 6, "key") == "[0]"


def test_jsonl_keyed_wrapped_single_brace(qtbot, opened, views, tmp_path: Path, lang_uz) -> None:
    # JSON Lines
    jl = tmp_path / "a.jsonl"
    jl.write_text("\n".join(json.dumps(r) for r in people(5)) + "\n", encoding="utf-8")
    v1 = views(opened(jl))
    qtbot.waitUntil(lambda: v1.row_count() == 5, timeout=TIMEOUT)
    assert labels(qtbot, v1) == ["#0", "#1", "#2", "#3", "#4"]
    # keyed: keys as labels, "#n" as tooltip
    keyed = write(tmp_path / "k.json", {"alpha": {"x": 1}, "beta": {"y": [1, 2]}, "gamma": {"z": None},
                                        "a\nb": {"w": True}})
    v2 = views(opened(keyed))
    qtbot.waitUntil(lambda: v2.row_count() == 4, timeout=TIMEOUT)
    assert labels(qtbot, v2) == ["alpha", "beta", "gamma", '"a\\nb"']
    assert v2.cell_at(1, "key").tooltip == "Yozuv #1"
    # wrapped: wrapper row first, then the records; the record array is not expandable
    wrapped = write(tmp_path / "w.json", {"meta": {"v": 1, "name": "x"}, "data": people(4), "tail": [1, 2]})
    s3 = opened(wrapped)
    assert s3.doc.kind == "wrapped"
    v3 = views(s3)
    qtbot.waitUntil(lambda: v3.row_count() == 5, timeout=TIMEOUT)
    assert labels(qtbot, v3) == ["(o'rama)", "#0", "#1", "#2", "#3"]
    assert v3.row_keypath(0) == (WRAPPER,) and v3.row_keypath(1) == (0,)
    assert v3.cell_at(0, "key").italic
    expand(qtbot, v3, 0)
    assert labels(qtbot, v3, 0, 4) == ["(o'rama)", "meta", "data", "tail"]
    data_row = 2
    assert v3.cell_at(data_row, "key").expander == 0
    assert wait_text(qtbot, v3, data_row, "value") == "[4] yozuvlar quyida"
    assert v3.cell_text(data_row, "children") == "4"
    assert not v3.expand_row(data_row)
    assert v3.row_count() == 8 and v3.row_keypath(4) == (0,)
    # single document: one "$" row, expanded on open
    single = write(tmp_path / "s.json", {"only": {"x": 1, "y": [1, 2, 3]}})
    s4 = opened(single)
    assert s4.doc.kind == "single"
    v4 = views(s4)
    qtbot.waitUntil(lambda: v4.row_count() == 2, timeout=TIMEOUT)
    assert labels(qtbot, v4) == ["$", "only"]
    assert v4.row_keypath(0) == (ROOT,) and v4.cell_at(0, "key").expander == 2
    # brace list
    brace = tmp_path / "b.json"
    brace.write_text('{ {"a": 1}, {"a": 2}, {"a": 3} }', encoding="utf-8")
    s5 = opened(brace)
    assert s5.doc.kind == "brace_list"
    v5 = views(s5)
    qtbot.waitUntil(lambda: v5.row_count() == 3, timeout=TIMEOUT)
    assert labels(qtbot, v5) == ["#0", "#1", "#2"]
    expand(qtbot, v5, 2)
    assert wait_text(qtbot, v5, 3, "value") == "3"


# ----------------------------------------------------------------------------------
# expansion kept across filter / sort / reset; live growth
# ----------------------------------------------------------------------------------
def wait_view(qtbot, s: DocSession, n: int) -> None:
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == n, timeout=TIMEOUT)


def test_expansion_kept_across_filter_sort_reset(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "p.json", people(30)))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 30, timeout=TIMEOUT)
    expand(qtbot, view, 20)
    expand(qtbot, view, 25)                    # contact of #20 (row 20 + 5)
    assert view.row_count() == 37
    s.set_filter(FilterSpec("id >= 10"))
    wait_view(qtbot, s, 20)
    # record 20 is at position 10 now and still expanded (with its expanded contact)
    qtbot.waitUntil(lambda: view.row_of((20,)) == 10 and view.row_count() == 27, timeout=TIMEOUT)
    assert wait_text(qtbot, view, 10, "key") == "#20" and wait_text(qtbot, view, 11, "key") == "id"
    assert view.flat.is_expanded((20, 4))
    s.set_sort([SortKey("id", descending=True)])
    wait_view(qtbot, s, 20)
    qtbot.waitUntil(lambda: view.row_of((20,)) == 9, timeout=TIMEOUT)
    assert wait_text(qtbot, view, 9, "key") == "#20" and view.row_count() == 27
    assert wait_text(qtbot, view, 0, "key") == "#29"
    # a filter without the record: no rows for it, but the expansion is remembered
    s.set_filter(FilterSpec("id < 5"))
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 5, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: view.row_count() == 5, timeout=TIMEOUT)
    assert view.row_of((20,)) is None and view.flat.is_expanded((20,))
    s.reset_view()
    wait_view(qtbot, s, 30)
    qtbot.waitUntil(lambda: view.row_of((20,)) == 20 and view.row_count() == 37, timeout=TIMEOUT)
    assert wait_text(qtbot, view, 26, "key") == "phone"


def slow_index(monkeypatch, delay: float = 0.004) -> None:
    """Index in 32 KiB pieces with a pause per batch (observable live growth)."""
    orig = Document._append_spans

    def slow(self, writer, out):
        orig(self, writer, out)
        time.sleep(delay)

    monkeypatch.setattr(Document, "_append_spans", slow)
    monkeypatch.setattr(reader_mod, "FEED_BYTES", 32 * 1024)


def test_live_growth_while_indexing(qtbot, opened, views, tmp_path: Path, monkeypatch) -> None:
    slow_index(monkeypatch)
    monkeypatch.setattr(DocSession, "COUNT_POLL_MS", 25)
    path = tmp_path / "big.json"
    n = 300_000
    path.write_bytes(b"[" + b",".join(b'{"i":%d,"g":%d}' % (i, i % 2) for i in range(n)) + b"]")
    s = opened(path, wait=False)
    view = views(s)
    seen: list[int] = []
    view.structure_changed.connect(lambda: seen.append(view.row_count()) if s.state == "indexing" else None)
    qtbot.waitUntil(lambda: s.state == "indexing" and view.row_count() > 0, timeout=TIMEOUT)
    assert s.state == "indexing"
    assert wait_text(qtbot, view, 0, "key") == "#0"
    expand(qtbot, view, 0)                     # records can be opened while indexing
    assert labels(qtbot, view, 1, 3) == ["i", "g"]
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        pass
    qtbot.waitUntil(lambda: view.row_count() == n + 2, timeout=TIMEOUT)
    growing = [c for c in seen if 0 < c < n]
    assert len(set(growing)) >= 2 and growing == sorted(growing)


# ----------------------------------------------------------------------------------
# large nodes (children task) and a 1 M-element array
# ----------------------------------------------------------------------------------
def test_large_node_expands_through_children_task(qtbot, app, opened, views, tmp_path: Path, lang_uz) -> None:
    app.settings.large_node_bytes = 1000
    path = write(tmp_path / "l.json", [{"big": list(range(2000)), "small": [1, 2]}, {"x": 1}])
    s = opened(path)
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 2, timeout=TIMEOUT)
    added: list = []
    app.tasks.task_added.connect(added.append)
    assert wait_text(qtbot, view, 0, "children") == "?"           # large: not counted by the loader
    assert view.cell_at(0, "children").tooltip
    assert wait_text(qtbot, view, 0, "value").startswith("{?} big: [")
    view.expand_row(0)
    assert view.is_loading((0,)) and view.cell_at(0, "key").expander == 3
    qtbot.waitUntil(lambda: view.flat.is_expanded((0,)), timeout=TIMEOUT)
    assert any(t.kind == "children" and t.title.startswith("Bolalar indekslanmoqda: [0]") for t in added)
    assert labels(qtbot, view, 1, 3) == ["big", "small"]
    assert view.cell_text(0, "children") == "2"
    assert wait_text(qtbot, view, 1, "children") == "?"
    expand(qtbot, view, 1)
    assert view.row_count() == 2 + 2 + 2000
    assert view.cell_text(1, "children") == "2 000"
    assert wait_text(qtbot, view, 2001, "key") == "[1999]" and view.cell_text(2001, "value") == "1999"
    assert sum(1 for t in added if t.kind == "children") == 2
    assert not app.errors


def test_children_task_cancel_leaves_node_collapsed(qtbot, app, opened, views, tmp_path: Path,
                                                    monkeypatch) -> None:
    app.settings.large_node_bytes = 1000
    s = opened(write(tmp_path / "c.json", [{"big": list(range(3000))}]))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 1, timeout=TIMEOUT)
    wait_text(qtbot, view, 0, "key")
    orig = Node.child_count
    release = threading.Event()

    def slow_count(self, progress=None, cancel=None):
        while cancel is not None and not release.is_set():
            cancel.check()
            time.sleep(0.005)
        return orig(self, progress=progress, cancel=cancel)

    monkeypatch.setattr(Node, "child_count", slow_count)
    view.expand_row(0)
    qtbot.waitUntil(lambda: any(t.kind == "children" for t in app.tasks.tasks(owner=view)), timeout=TIMEOUT)
    task = next(t for t in app.tasks.tasks(owner=view) if t.kind == "children")
    assert not task.silent and view.is_loading((0,))
    task.cancel()
    qtbot.waitUntil(lambda: not view.is_loading((0,)), timeout=TIMEOUT)
    assert not view.flat.is_expanded((0,)) and view.cell_at(0, "key").expander == 1
    assert not app.errors
    release.set()


def test_million_element_array_expands_fast(qtbot, opened, views, tmp_path: Path, capsys) -> None:
    n = 1_000_000
    path = tmp_path / "million.json"
    path.write_text('[{"big": ' + json.dumps(list(range(n))) + ', "after": 1}, {"x": 2}]', encoding="utf-8")
    s = opened(path)
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 2, timeout=TIMEOUT)
    expand(qtbot, view, 0)
    t0 = time.perf_counter()
    expand(qtbot, view, 1)                    # indexes the 1 M children in a task
    indexed = time.perf_counter() - t0
    assert view.row_count() == 2 + 2 + n
    view.collapse_row(1)
    assert view.row_count() == 4
    # children are indexed now: expanding is immediate (virtual rows); nothing cached
    view._loader.invalidate()
    view._src.clear()
    assert wait_text(qtbot, view, 1, "children") == "1 000 000"   # the row's own data (count known)
    t0 = time.perf_counter()
    assert view.expand_node((0, 0)) is True
    sync = time.perf_counter() - t0
    assert view.row_count() == 4 + n
    qtbot.waitUntil(lambda: view.cell_text(2, "key") == "[0]" and view.cell_text(30, "key") == "[28]",
                    timeout=TIMEOUT)
    shown = time.perf_counter() - t0
    # jump to the end: the rows there are loaded on demand
    view.select_row(n + 1, scroll="center")
    qtbot.waitUntil(lambda: view.cell_text(n + 1, "key") == f"[{n - 1}]", timeout=TIMEOUT)
    with capsys.disabled():
        print(f"\n[tree] 1M children: indexed+expanded {indexed * 1000:.0f} ms; re-expand {sync * 1000:.1f} ms; "
              f"first rows shown {shown * 1000:.0f} ms")
    assert sync < 0.2
    assert shown < 2.0


# ----------------------------------------------------------------------------------
# expand all / expand to level / collapse all
# ----------------------------------------------------------------------------------
def nested(n_items: int) -> dict:
    return {"items": [{"a": i, "b": [i, i + 1]} for i in range(n_items)], "meta": {"x": {"y": 1}}}


def test_expand_all_confirm_cap_levels_collapse(qtbot, app, opened, views, tmp_path: Path, monkeypatch,
                                                lang_uz) -> None:
    from jsonreshape.gui import treeview as tv

    s = opened(write(tmp_path / "n.json", [nested(400), nested(2), {"z": 1}]))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 3, timeout=TIMEOUT)
    asked: list[str] = []
    answer = {"value": False}
    monkeypatch.setattr(widgets, "confirm_handler",
                        lambda parent, text, title: asked.append(text) or answer["value"])
    wait_text(qtbot, view, 0, "key")
    view.select_row(0)
    # items + meta, 400 items, 2 members each, 2 elements each, x, y
    expected_rows = 2 + 400 + 400 * 2 + 400 * 2 + 1 + 1
    view.expand_all_current()
    qtbot.waitUntil(lambda: bool(asked), timeout=TIMEOUT)
    assert f"{expected_rows:,}".replace(",", " ") + " ta qator" in asked[0]
    wait_idle(qtbot, view)
    assert view.row_count() == 3                       # "no": nothing expanded
    answer["value"] = True
    view.expand_all((0,))
    qtbot.waitUntil(lambda: view.row_count() == 3 + expected_rows, timeout=TIMEOUT)
    assert len(asked) == 2
    assert view.flat.is_expanded((0, 0, 399, 1)) and view.flat.is_expanded((0, 1, 0))
    # a small subtree needs no confirmation
    view.expand_all((1,))
    qtbot.waitUntil(lambda: view.flat.is_expanded((1, 0, 1, 1)), timeout=TIMEOUT)
    assert len(asked) == 2
    # collapse all: every expansion forgotten
    view.collapse_all()
    assert view.row_count() == 3 and not view.flat.expanded_keypaths()
    # the hard cap refuses with a message
    monkeypatch.setattr(tv, "EXPAND_CAP_ROWS", 500)
    view.expand_all((0,))
    qtbot.waitUntil(lambda: view.message_bar.isVisible(), timeout=TIMEOUT)
    assert "500" in view.message_bar.message() and view.row_count() == 3
    monkeypatch.setattr(tv, "EXPAND_CAP_ROWS", EXPAND_CAP_ROWS)
    # expand to level, relative to the current node
    view.select_row(1)
    view.expand_to_level(2)
    qtbot.waitUntil(lambda: view.flat.is_expanded((1, 0)), timeout=TIMEOUT)
    wait_idle(qtbot, view)
    assert view.flat.is_expanded((1,)) and view.flat.is_expanded((1, 1))
    assert not view.flat.is_expanded((1, 0, 0)) and not view.flat.is_expanded((1, 1, 0))
    view.expand_to_level(3, (1,))
    qtbot.waitUntil(lambda: view.flat.is_expanded((1, 0, 1)), timeout=TIMEOUT)
    wait_idle(qtbot, view)
    view.expand_to_level(1, (1,))                       # deeper levels are collapsed again
    qtbot.waitUntil(lambda: not view.flat.is_expanded((1, 0)), timeout=TIMEOUT)
    assert view.flat.is_expanded((1,)) and view.row_count() == 3 + 2
    # top level (no current node): every record to level 1
    view.collapse_all()
    view.grid.set_current(-1)
    view._cur_kp = None
    view.expand_to_level(1)
    qtbot.waitUntil(lambda: all(view.flat.is_expanded((i,)) for i in range(3)), timeout=TIMEOUT)
    assert view.row_count() == 3 + 2 + 2 + 1
    # collapse the subtree of one node (context menu "collapse all")
    view.collapse_subtree((0,))
    assert not view.flat.is_expanded((0,)) and view.row_count() == 3 + 2 + 1


# ----------------------------------------------------------------------------------
# selection -> session.current, reveal, breadcrumb
# ----------------------------------------------------------------------------------
def test_selection_sets_session_current_every_format(qtbot, opened, views, tmp_path: Path) -> None:
    # array
    s = opened(write(tmp_path / "a.json", people(5)))
    v = views(s)
    qtbot.waitUntil(lambda: v.row_count() == 5, timeout=TIMEOUT)
    expand(qtbot, v, 1)
    expand(qtbot, v, 6)                       # contact
    v.select_row(row_by_label(qtbot, v, "zip", 6))
    qtbot.waitUntil(lambda: s.current == NodeRef(1, ("contact", "zip"), (4, 1)), timeout=TIMEOUT)
    assert v.current_keypath() == (1, 4, 1) and v.selection_count() == 1
    v.select_row(1)
    qtbot.waitUntil(lambda: s.current == NodeRef(1, (), ()), timeout=TIMEOUT)
    # jsonl
    jl = tmp_path / "a.jsonl"
    jl.write_text("\n".join(json.dumps(r) for r in people(3)), encoding="utf-8")
    s2 = opened(jl)
    v2 = views(s2)
    qtbot.waitUntil(lambda: v2.row_count() == 3, timeout=TIMEOUT)
    expand(qtbot, v2, 2)
    expand(qtbot, v2, 6)                      # tags of #2
    v2.select_row(8)
    qtbot.waitUntil(lambda: s2.current == NodeRef(2, ("tags", 1), (3, 1)), timeout=TIMEOUT)
    # keyed
    s3 = opened(write(tmp_path / "k.json", {"alpha": {"x": 1}, "beta": {"y": [5, 6]}, "gamma": {"z": None}}))
    v3 = views(s3)
    qtbot.waitUntil(lambda: v3.row_count() == 3, timeout=TIMEOUT)
    expand(qtbot, v3, 1)
    expand(qtbot, v3, 2)
    v3.select_row(4)
    qtbot.waitUntil(lambda: s3.current == NodeRef(1, ("y", 1), (0, 1)), timeout=TIMEOUT)
    assert s3.absolute_path(s3.current) == ("beta", "y", 1)
    # wrapped: wrapper members are absolute refs, records are record refs
    s4 = opened(write(tmp_path / "w.json", {"meta": {"v": 1}, "data": people(3)}))
    v4 = views(s4)
    qtbot.waitUntil(lambda: v4.row_count() == 4, timeout=TIMEOUT)
    expand(qtbot, v4, 0)
    expand(qtbot, v4, 1)                      # meta
    v4.select_row(2)
    qtbot.waitUntil(lambda: s4.current == NodeRef(None, ("meta", "v"), (0, 0)), timeout=TIMEOUT)
    v4.select_row(3)                          # the data member
    qtbot.waitUntil(lambda: s4.current == NodeRef(None, ("data",), (1,)), timeout=TIMEOUT)
    v4.select_row(5)                          # record #1
    qtbot.waitUntil(lambda: s4.current == NodeRef(1, (), ()), timeout=TIMEOUT)
    assert s4.absolute_path(s4.current) == ("data", 1)
    v4.select_row(0)
    qtbot.waitUntil(lambda: s4.current == NodeRef(None, (), ()), timeout=TIMEOUT)
    # single
    s5 = opened(write(tmp_path / "s.json", {"only": {"x": 1, "y": [1, 2, 3]}}))
    v5 = views(s5)
    qtbot.waitUntil(lambda: v5.row_count() == 2, timeout=TIMEOUT)
    expand(qtbot, v5, 1)
    expand(qtbot, v5, 3)
    v5.select_row(5)
    qtbot.waitUntil(lambda: s5.current == NodeRef(None, ("only", "y", 1), (0, 1, 1)), timeout=TIMEOUT)


def test_follow_and_reveal_key_only_refs(qtbot, app, opened, views, tmp_path: Path, lang_uz) -> None:
    s = opened(write(tmp_path / "p.json", people(60)))
    view = views(s)
    view.resize(1000, 300)
    qtbot.waitUntil(lambda: view.row_count() == 60, timeout=TIMEOUT)
    # a search result: record + keys only -> resolved, ancestors expanded, centered
    s.select(NodeRef(45, ("contact", "zip")), reveal=True)
    qtbot.waitUntil(lambda: view.current_keypath() == (45, 4, 1), timeout=TIMEOUT)
    row = view.row_of((45, 4, 1))
    assert view.grid.current()[0] == row
    first, last = view.grid.visible_rows()
    assert first < row < last and abs((first + last) // 2 - row) <= 2
    assert s.current == NodeRef(45, ("contact", "zip"), (4, 1))   # the ipath was added
    assert len(s.history()) == 1
    # another view changes the current node: the tree follows
    s.select(NodeRef(3, ("tags", 1)), origin=object())
    qtbot.waitUntil(lambda: view.current_keypath() == (3, 3, 1), timeout=TIMEOUT)
    assert wait_text(qtbot, view, view.row_of((3, 3, 1)), "value") == '"b"'
    # a path that does not exist: a message, nothing changes
    s.select(NodeRef(3, ("nope",)), origin=object())
    qtbot.waitUntil(lambda: bool(app.statuses), timeout=TIMEOUT)
    assert view.current_keypath() == (3, 3, 1)
    # a record that is not in the current view
    s.set_filter(FilterSpec("id < 10"))
    wait_view(qtbot, s, 10)
    qtbot.waitUntil(lambda: view.row_count() < 30, timeout=TIMEOUT)
    app.statuses.clear()
    s.select(NodeRef(45, ()), reveal=True, origin=object())
    msg = "Bu yozuv joriy ko'rinishda yo'q"
    qtbot.waitUntil(lambda: msg in app.statuses, timeout=TIMEOUT)
    assert view.last_message == msg
    # a record that is in the filtered view: found through its view position
    s.select(NodeRef(7, ("contact",)), reveal=True, origin=object())
    qtbot.waitUntil(lambda: view.current_keypath() == (7, 4), timeout=TIMEOUT)


def test_reveal_wrapped_absolute_ref_and_hidden_view(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "w.json", {"meta": {"v": 1, "deep": {"k": [1, 2]}}, "data": people(5)}))
    view = views(s, activate=False)
    view.hide()
    s.select(NodeRef(None, ("data", 3, "name")), reveal=True)     # while the tree is hidden: no I/O
    assert view.row_count() == 0
    view.show()
    view.activate()
    qtbot.waitUntil(lambda: view.current_keypath() == (3, 1), timeout=TIMEOUT)
    assert s.current == NodeRef(None, ("data", 3, "name"))         # the other view's ref is kept
    s.select(NodeRef(None, ("meta", "deep", "k", 1)), reveal=True)
    qtbot.waitUntil(lambda: view.current_keypath() == (WRAPPER, 0, 1, 0, 1), timeout=TIMEOUT)
    assert view.flat.is_expanded((WRAPPER,)) and view.flat.is_expanded((WRAPPER, 0, 1, 0))


def test_breadcrumb_segments_click_and_elision(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "p.json", people(5)))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 5, timeout=TIMEOUT)
    s.select(NodeRef(2, ("contact", "phone")), reveal=True)
    qtbot.waitUntil(lambda: view.current_keypath() == (2, 4, 0), timeout=TIMEOUT)
    assert view.breadcrumb.segments() == ["$", "#2", "contact", "phone"]
    refs = view.crumb_refs()
    assert refs[0] is None and refs[1] == NodeRef(2, (), ())
    view.breadcrumb.click(2)                                     # "contact"
    qtbot.waitUntil(lambda: s.current == NodeRef(2, ("contact",), (4,)), timeout=TIMEOUT)
    assert view.current_keypath() == (2, 4)
    assert view.breadcrumb.segments() == ["$", "#2", "contact"]
    view.breadcrumb.click(0)                                     # "$" of an array: not a row
    assert view.current_keypath() == (2, 4)
    # copy button = dotted path
    view.breadcrumb.copy_requested.emit()
    assert QGuiApplication.clipboard().text() == "[2].contact"
    # wrapped document, long path elided in the middle
    deep = {"a": {"b": {"c": {"d": {"e": 1}}}}}
    s2 = opened(write(tmp_path / "w.json", {"meta": {"v": 1}, "data": [deep, {"x": 1}]}))
    assert s2.doc.kind == "wrapped"
    v2 = views(s2)
    v2.resize(260, 400)
    qtbot.waitUntil(lambda: v2.row_count() == 3, timeout=TIMEOUT)
    s2.select(NodeRef(0, ("a", "b", "c", "d", "e")), reveal=True)
    qtbot.waitUntil(lambda: v2.current_keypath() == (0, 0, 0, 0, 0, 0), timeout=TIMEOUT)
    assert v2.breadcrumb.segments() == ["$", "data", "#0", "a", "b", "c", "d", "e"]
    qtbot.waitUntil(lambda: bool(v2.breadcrumb.hidden_segments()), timeout=TIMEOUT)
    hidden = v2.breadcrumb.hidden_segments()
    assert 0 not in hidden and 7 not in hidden
    v2.breadcrumb.click(1)                                       # "data": the wrapper member
    qtbot.waitUntil(lambda: v2.current_keypath() == (WRAPPER, 1), timeout=TIMEOUT)
    assert s2.current == NodeRef(None, ("data",), (1,))


def test_keyboard_navigation(qtbot, opened, views, tmp_path: Path, monkeypatch) -> None:
    from jsonreshape.gui import actions

    s = opened(write(tmp_path / "p.json", people(5)))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 5, timeout=TIMEOUT)
    wait_text(qtbot, view, 0, "key")
    grid = view.grid
    grid.setFocus()
    assert grid.current()[0] == -1                       # nothing selected on open
    qtbot.keyClick(grid, Qt.Key.Key_Down)
    assert grid.current()[0] == 0
    qtbot.waitUntil(lambda: s.current == NodeRef(0, (), ()), timeout=TIMEOUT)
    qtbot.keyClick(grid, Qt.Key.Key_Right)               # expand
    qtbot.waitUntil(lambda: view.flat.is_expanded((0,)), timeout=TIMEOUT)
    qtbot.keyClick(grid, Qt.Key.Key_Right)               # first child
    assert grid.current()[0] == 1 and view.current_keypath() == (0, 0)
    qtbot.keyClick(grid, Qt.Key.Key_Left)                # to the parent
    assert view.current_keypath() == (0,)
    qtbot.keyClick(grid, Qt.Key.Key_Left)                # collapse
    assert not view.flat.is_expanded((0,)) and view.row_count() == 5
    qtbot.keyClick(grid, Qt.Key.Key_Asterisk, Qt.KeyboardModifier.ShiftModifier)   # expand all
    qtbot.waitUntil(lambda: view.flat.is_expanded((0, 4)), timeout=TIMEOUT)
    assert view.flat.is_expanded((0,))
    qtbot.keyClick(grid, Qt.Key.Key_Minus)
    assert not view.flat.is_expanded((0,))
    qtbot.keyClick(grid, Qt.Key.Key_Plus)
    assert view.flat.is_expanded((0,))
    # stage 3 (§12.9): Enter edits -- a container opens the JSON editor -- and a double click
    # toggles a container (this test pinned "Enter toggles" in stage 2)
    qtbot.waitUntil(lambda: s.edit is not None and s.edit.available, timeout=TIMEOUT)
    dialogs: list = []
    monkeypatch.setattr(TreeView, "_json_dialog", lambda self, ref, info: dialogs.append(ref))
    qtbot.keyClick(grid, Qt.Key.Key_Return)
    assert dialogs == [NodeRef(0, (), ())] and view.flat.is_expanded((0,))
    from PySide6.QtTest import QTest
    QTest.mouseDClick(grid.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                      grid.cell_rect(0, 1).center())
    assert not view.flat.is_expanded((0,))
    QTest.mouseDClick(grid.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                      grid.cell_rect(0, 1).center())
    assert view.flat.is_expanded((0,))
    shown: list = []
    monkeypatch.setattr(actions, "show_full_value", lambda parent, session, ref: shown.append(ref))
    qtbot.keyClick(grid, Qt.Key.Key_Space)
    assert shown == [NodeRef(0, (), ())]
    # Ctrl++ still zooms (the tree only takes plain keys)
    before = grid.zoom_level()
    qtbot.keyClick(grid, Qt.Key.Key_Plus, Qt.KeyboardModifier.ControlModifier)
    assert grid.zoom_level() > before


# ----------------------------------------------------------------------------------
# node sorting
# ----------------------------------------------------------------------------------
UZ_KEYS = ["z", "o'rik", "olma", "g'isht", "sh", "ch", "b", "child10", "child2", "Apple", "apple"]


def test_sort_keys_alphabetically_uzbek_order(qtbot, app, opened, views, tmp_path: Path) -> None:
    from jsonreshape.sortkeys import collation_key

    rec = {k: i for i, k in enumerate(UZ_KEYS)}
    rec["nested"] = {"y": 1, "x": 2}
    s = opened(write(tmp_path / "k.json", [rec, {"b": 1, "a": 2}]))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 2, timeout=TIMEOUT)
    expand(qtbot, view, 0)
    n = len(UZ_KEYS) + 1
    assert labels(qtbot, view, 1, 1 + n) == UZ_KEYS + ["nested"]       # file order
    view.toggle_sort_keys(True)
    assert view.sort_keys_enabled()
    expected = sorted(UZ_KEYS + ["nested"], key=collation_key)
    qtbot.waitUntil(lambda: [view.cell_text(r, "key") for r in range(1, 1 + n)] == expected, timeout=TIMEOUT)
    # Uzbek alphabet: ... z, o', g', sh, ch, ng; natural digit runs; lower case before upper
    assert expected.index("z") < expected.index("o'rik") < expected.index("g'isht") \
        < expected.index("sh") < expected.index("ch")
    assert expected.index("child2") < expected.index("child10")
    assert expected.index("apple") + 1 == expected.index("Apple")
    # the values stay with their keys; a selected row has its FILE index in the ipath
    row = view.row_of((0, UZ_KEYS.index("sh")))
    assert view.cell_text(row, "value") == str(UZ_KEYS.index("sh"))
    view.select_row(row)
    qtbot.waitUntil(lambda: s.current == NodeRef(0, ("sh",), (UZ_KEYS.index("sh"),)), timeout=TIMEOUT)
    # objects expanded later are sorted too (also the next record)
    expand(qtbot, view, view.row_of((0, n - 1)))
    nested_row = view.row_of((0, n - 1))
    assert labels(qtbot, view, nested_row + 1, nested_row + 3) == ["x", "y"]
    second = view.row_of((1,))
    expand(qtbot, view, second)
    assert labels(qtbot, view, second + 1, second + 3) == ["a", "b"]
    # off: file order again everywhere
    view.toggle_sort_keys(False)
    assert labels(qtbot, view, 1, 1 + n) == UZ_KEYS + ["nested"]
    assert labels(qtbot, view, view.row_of((1,)) + 1, view.row_of((1,)) + 3) == ["b", "a"]
    assert not app.errors


def test_sort_array_elements_by_field(qtbot, app, opened, views, tmp_path: Path) -> None:
    items = [{"n": 3, "s": "c"}, {"n": None}, {"n": 1, "s": "a"}, {}, {"n": 2, "s": "b"}, {"n": "x"}]
    s = opened(write(tmp_path / "e.json", [{"items": items, "nums": [5, 1, 4]}, {"x": 1}]))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 2, timeout=TIMEOUT)
    expand(qtbot, view, 0)
    expand(qtbot, view, 1)
    kp = (0, 0)
    assert view.sort_elements(kp, "n")

    def order() -> list[str]:
        return [view.cell_text(r, "key") for r in range(2, 2 + len(items))]

    # numbers < strings, then null, then missing (stable); indices are the file indices
    qtbot.waitUntil(lambda: order() == ["[2]", "[4]", "[0]", "[5]", "[1]", "[3]"], timeout=TIMEOUT)
    assert view.element_sort(kp) == ("n", False)
    view.select_row(2)
    qtbot.waitUntil(lambda: s.current == NodeRef(0, ("items", 2), (0, 2)), timeout=TIMEOUT)
    view.sort_elements(kp, "n", descending=True)
    qtbot.waitUntil(lambda: order() == ["[5]", "[0]", "[4]", "[2]", "[1]", "[3]"], timeout=TIMEOUT)
    # collapse / expand keeps the element order
    view.collapse_node(kp)
    view.expand_node(kp)
    qtbot.waitUntil(lambda: view.flat.is_expanded(kp), timeout=TIMEOUT)
    assert order() == ["[5]", "[0]", "[4]", "[2]", "[1]", "[3]"]
    view.original_order(kp)
    assert order() == [f"[{i}]" for i in range(len(items))] and view.element_sort(kp) is None
    # "$" for scalar elements, through the dialog
    nums = view.row_of((0, 1))
    view.ask_sort_elements((0, 1))
    qtbot.waitUntil(lambda: view._sort_dialog is not None, timeout=TIMEOUT)
    dlg = view._sort_dialog
    assert [dlg.field_combo.itemText(i) for i in range(dlg.field_combo.count())] == ["$"]
    dlg.desc_radio.setChecked(True)
    dlg.accept()
    qtbot.waitUntil(lambda: view.flat.is_expanded((0, 1)), timeout=TIMEOUT)
    qtbot.waitUntil(lambda: [view.cell_text(r, "value") for r in range(nums + 1, nums + 4)] == ["5", "4", "1"],
                    timeout=TIMEOUT)
    # an invalid field is reported, nothing changes
    assert not view.sort_elements(kp, "a[")
    assert app.errors and view.element_sort(kp) is None


def test_element_sort_dialog_fields(qtbot) -> None:
    dlg = ElementSortDialog(["$", "name", "[\"a b\"]"], current=("name", True), path="data[0].items")
    qtbot.addWidget(dlg)
    assert dlg.field() == "name" and dlg.descending()
    dlg.field_combo.setEditText(" city ")
    dlg.asc_radio.setChecked(True)
    assert dlg.field() == "city" and not dlg.descending()


# ----------------------------------------------------------------------------------
# quick find
# ----------------------------------------------------------------------------------
def test_quick_find_highlights_f3_wraps_and_status(qtbot, opened, views, tmp_path: Path, monkeypatch,
                                                    lang_uz) -> None:
    from jsonreshape.gui import treeview as tv

    s = opened(write(tmp_path / "p.json", people(20)))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 20, timeout=TIMEOUT)
    expand(qtbot, view, 3)
    expand(qtbot, view, 12)                    # record #7 (row 7 + 5)
    view.quick_find()
    assert view.find_bar.isVisible()
    # what the rows show is searched: the previews of all 20 (collapsed) records and the
    # name rows of the two expanded ones (C4: previews used to be skipped)
    view.find_bar.open_bar("user")
    want = {(i,) for i in range(20)} | {(3, 1), (7, 1)}
    qtbot.waitUntil(lambda: set(view.quick_find_matches()) == want, timeout=TIMEOUT)
    view.find_bar.open_bar("user7")
    qtbot.waitUntil(lambda: set(view.quick_find_matches()) == {(7,), (7, 1)}, timeout=TIMEOUT)
    top7, r7 = view.row_of((7,)), view.row_of((7, 1))
    qtbot.waitUntil(lambda: view.grid.current()[0] == top7, timeout=TIMEOUT)
    assert view.find_bar.status_text() == "1 / 2"
    preview = view.cell_at(top7, "value")
    at = preview.text.index("user7")
    assert preview.text.startswith("{5} id: 7") and preview.spans == ((at, at + 5, True),)
    assert view.cell_at(r7, "value").spans == ((1, 6),)
    view.find_next()
    assert view.grid.current()[0] == r7 and view.find_bar.status_text() == "2 / 2"
    cell = view.cell_at(r7, "value")
    assert cell.text == '"user7"' and cell.spans == ((1, 6, True),)
    view.find_next()                            # wraps
    assert view.grid.current()[0] == top7
    view.find_next(backwards=True)              # wraps backwards
    assert view.grid.current()[0] == r7 and s.current == NodeRef(7, ("name",), (1,))
    # keys are searched too; a collapsed "contact" is found by its preview ("phone: …")
    view.find_bar.open_bar("phon")
    qtbot.waitUntil(lambda: set(view.quick_find_matches()) == {(3, 4), (7, 4)}, timeout=TIMEOUT)
    assert view.quick_find_matches()[(7, 4)][0] == ()          # the value (preview), not the key
    expand(qtbot, view, view.row_of((7, 4)))
    qtbot.waitUntil(lambda: set(view.quick_find_matches()) == {(3, 4), (7, 4), (7, 4, 0)},
                    timeout=TIMEOUT)            # rescan after the expansion
    row = view.row_of((7, 4, 0))
    assert view.cell_at(row, "key").spans[0][:2] == (0, 4)
    # the scan limit shows the capped note
    monkeypatch.setattr(tv, "QUICK_SCAN_ROWS", 3)
    view.find_bar.open_bar("user")
    qtbot.waitUntil(lambda: "+" in view.find_bar.status_text(), timeout=TIMEOUT)
    # closing clears the highlights
    view.find_bar.close_bar()
    assert view.quick_find_matches() == {} and view.cell_at(r7, "value").spans == ()


# ----------------------------------------------------------------------------------
# context menu, full value, selected text
# ----------------------------------------------------------------------------------
def test_context_menu_extra_items(qtbot, opened, views, tmp_path: Path) -> None:
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QMenu

    s = opened(write(tmp_path / "w.json", {"meta": {"v": 1}, "data": people(3)}))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 4, timeout=TIMEOUT)
    wait_text(qtbot, view, 1, "key")
    menu = view.context_menu(1)
    names = {a.objectName() for a in menu.findChildren(QAction)}
    for name in ("copy_value_json", "copy_path_dotted", "filter_by_value", "full_value", "show_in_tree",
                 "tree_expand_all", "tree_collapse_all", "tree_sort_keys", "tree_sort_elements",
                 "tree_original_order") + tuple(f"tree_expand_level_{n}" for n in range(1, 10)):
        assert name in names, name
    menus = {m.objectName() for m in menu.findChildren(QMenu)}
    assert {"tree_expand_level", "tree_sort_menu"} <= menus
    act = {a.objectName(): a for a in menu.findChildren(QAction)}
    assert act["tree_expand_all"].isEnabled() and not act["tree_sort_elements"].isEnabled()
    assert not act["tree_original_order"].isEnabled() and not act["show_in_tree"].isEnabled()
    act["tree_expand_level_2"].trigger()          # row 1 = record #0 (row 0 is the wrapper)
    qtbot.waitUntil(lambda: view.flat.is_expanded((0,)) and view.flat.is_expanded((0, 4)), timeout=TIMEOUT)
    menu.deleteLater()
    # the wrapper row; its record array member cannot be expanded inline
    expand(qtbot, view, 0)
    wait_text(qtbot, view, 2, "key")
    data_menu = view.context_menu(2)
    act = {a.objectName(): a for a in data_menu.findChildren(QAction)}
    assert not act["tree_expand_all"].isEnabled() and act["tree_expand_all"].toolTip()
    data_menu.deleteLater()
    # a scalar
    expand(qtbot, view, 1)
    row = view.row_of((WRAPPER, 0, 0))
    wait_text(qtbot, view, row, "key")
    scalar_menu = view.context_menu(row)
    act = {a.objectName(): a for a in scalar_menu.findChildren(QAction)}
    assert not act["tree_expand_all"].isEnabled() and not act["tree_collapse_all"].isEnabled()
    scalar_menu.deleteLater()
    # the toggle in the sort submenu drives toggle_sort_keys
    m = view.context_menu(view.row_of((0,)))
    act = {a.objectName(): a for a in m.findChildren(QAction)}
    act["tree_sort_keys"].trigger()
    assert view.sort_keys_enabled()
    m.deleteLater()


def test_full_value_dialog_for_long_string(qtbot, opened, views, tmp_path: Path) -> None:
    long = "Toshkent " * 600 + "END"
    s = opened(write(tmp_path / "l.json", [{"text": long, "esc": "то\n\"q\""}, {"x": 1}]))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 2, timeout=TIMEOUT)
    expand(qtbot, view, 0)
    shown = wait_text(qtbot, view, 1, "value")
    assert shown.startswith('"Toshkent Toshkent') and shown.endswith("…") and len(shown) < 400
    assert wait_text(qtbot, view, 2, "value") == '"то\\n\\"q\\""'
    view.select_row(2)
    qtbot.waitUntil(lambda: view.selected_text() == "то\n\"q\"", timeout=TIMEOUT)
    view.select_row(1)
    assert view.selected_text() == "text"           # a cut string: its key
    dlg = view.show_full_value()                    # non-modal, deleted when closed
    qtbot.waitUntil(dlg.is_loaded, timeout=TIMEOUT)
    assert dlg.text() == long
    dlg.close()


# ----------------------------------------------------------------------------------
# the GUI thread never calls Node methods
# ----------------------------------------------------------------------------------
def test_no_node_calls_on_the_gui_thread(qtbot, app, opened, views, tmp_path: Path, monkeypatch) -> None:
    app.settings.large_node_bytes = 2000
    data = {"meta": {"v": 1}, "data": [{"big": list(range(1500)), "obj": {"b": 1, "a": 2}, "s": "x" * 50}
                                        for _ in range(3)]}
    s = opened(write(tmp_path / "w.json", data))
    on_gui: list[str] = []
    main = threading.main_thread()

    def guard(name, fn):
        def wrapped(*args, **kwargs):
            if threading.current_thread() is main:
                on_gui.append(name)
            return fn(*args, **kwargs)
        return wrapped

    for name in ("child_count", "children", "child", "find_child", "find_child_index", "value", "scalar_text",
                 "raw"):
        monkeypatch.setattr(Node, name, guard(name, getattr(Node, name)))
    for name in ("record_node", "root", "node", "records_node"):
        monkeypatch.setattr(Document, name, guard(name, getattr(Document, name)))
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 4, timeout=TIMEOUT)
    expand(qtbot, view, 0)                       # wrapper
    expand(qtbot, view, view.row_of((0,)))       # a record
    big = view.row_of((0, 0))
    expand(qtbot, view, big)                     # a large node: children task
    view.toggle_sort_keys(True)
    expand(qtbot, view, view.row_of((0, 1)))
    view.sort_elements((0, 0), "$", descending=True)
    qtbot.waitUntil(lambda: view.element_sort((0, 0)) is not None and not view.busy(), timeout=TIMEOUT)
    s.select(NodeRef(2, ("obj", "a")), reveal=True, origin=object())
    qtbot.waitUntil(lambda: view.current_keypath() == (2, 1, 1), timeout=TIMEOUT)
    view.find_bar.open_bar("1")
    qtbot.waitUntil(lambda: bool(view.quick_find_matches()), timeout=TIMEOUT)
    menu = view.context_menu(view.row_of((2, 1, 1)))
    menu.deleteLater()
    view.expand_all((1,))
    qtbot.waitUntil(lambda: view.flat.is_expanded((1, 1)), timeout=TIMEOUT)
    wait_idle(qtbot, view)
    assert on_gui == []
    assert not app.errors


# ----------------------------------------------------------------------------------
# reopen / shutdown
# ----------------------------------------------------------------------------------
def test_reopen_resets_and_shutdown_stops(qtbot, app, opened, views, tmp_path: Path) -> None:
    path = write(tmp_path / "p.json", people(4))
    s = opened(path)
    view = views(s)
    qtbot.waitUntil(lambda: view.row_count() == 4, timeout=TIMEOUT)
    expand(qtbot, view, 1)
    write(path, people(6))
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen()
    qtbot.waitUntil(lambda: view.row_count() == 6, timeout=TIMEOUT)
    assert not view.flat.expanded_keypaths() and wait_text(qtbot, view, 5, "key") == "#5"
    view.shutdown()
    view.shutdown()                              # idempotent
    s.select(NodeRef(2, ()), reveal=True)        # nothing calls back into a closed view
    assert app.tasks.wait_idle(10)


def test_value_column_fills_the_pane_until_resized(qtbot, opened, views, tmp_path: Path) -> None:
    s = opened(write(tmp_path / "p.json", people(3)))
    view = views(s)
    view.resize(700, 300)
    qtbot.waitUntil(lambda: view.row_count() == 3, timeout=TIMEOUT)

    def widths() -> dict[str, int]:
        return {c.id: c.width for c in view.grid.columns()}

    def shown_width() -> int:
        return sum(c.width for c in view.grid.columns() if c.visible)

    vp = view.grid.viewport()
    qtbot.waitUntil(lambda: 0 < vp.width() - shown_width() <= 2, timeout=TIMEOUT)   # no horizontal scrolling
    w = widths()
    assert (w["key"], w["children"], w["size"]) == (200, 70, 80) and not view.grid.columns()[4].visible
    view.resize(900, 300)
    qtbot.waitUntil(lambda: 0 < vp.width() - shown_width() <= 2, timeout=TIMEOUT)
    assert widths()["value"] > w["value"]
    # showing the hidden offset column keeps everything in the pane
    view.grid.set_column_visible(4, True)
    assert 0 < vp.width() - shown_width() <= 2
    # a manual resize stops the fitting
    view.grid.set_column_width(0, 260)
    before = widths()["value"]
    view.resize(800, 300)
    qtbot.wait(50)
    assert widths()["value"] == before and widths()["key"] == 260
