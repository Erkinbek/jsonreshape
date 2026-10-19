"""The structure builder view (jsonreshape/gui/mapview.py + gui/mapmodel.py; docs/ARCHITECTURE.md §13.5,
AGENT_PROMPT §7, §11, §14 tests 1-9 through the GUI).

* the tree is built for every document kind (array, JSON Lines, concatenated values, brace list, wrapped, keyed,
  single) from the sample in a silent task and equals ``mapping.infer_tree`` of the same records; rows like the
  prototype (labels, chips, meta, badges, samples, expansion), 400 children then a "more" row;
* every operation through the row buttons, the keys, the context menu and drag & drop (real mouse events at
  chosen fractions of the target row: before / inside / after / forbidden), undo / redo / reset, the stats line,
  the badges of moved / renamed / gathered / broadcast / group / recursive / merged nodes;
* the preview (Natija / Asl, 0-based navigation, following the session's current record, limit 21 markers,
  refresh after an edit), save -> open of mapping files, Full scan, reopen rules;
* no data parsed or read in bulk on the GUI thread.

The export dialog's mapping mode and the main window routing (Ctrl+Z / Ctrl+Y / Ctrl+E, the schema view's
"Tuzilma quruvchi…") are in test_gui_mapview_shell.py.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QAbstractSpinBox, QApplication, QLineEdit  # noqa: E402

from jsonreshape import codec, engine  # noqa: E402
from jsonreshape import mapping as mp  # noqa: E402
from jsonreshape.gui import mapmodel, widgets  # noqa: E402
from jsonreshape.gui.context import AppContext  # noqa: E402
from jsonreshape.gui.guistate import GuiState  # noqa: E402
from jsonreshape.gui.mapview import MapView  # noqa: E402
from jsonreshape.gui.session import DocSession, NodeRef  # noqa: E402

pytestmark = pytest.mark.gui

TIMEOUT = 30000
K = Qt.Key
M = Qt.KeyboardModifier
LEFT = Qt.MouseButton.LeftButton

SAMPLE = [
    {"id": 1, "name": "parent1", "statusComment": None,
     "contact": {"phone": "+998 90 123 45 67", "address": {"city": "Toshkent", "street": "Navoiy 12"}},
     "children": [
         {"id": 11, "name": "child1", "statusComment": None, "children": [
             {"id": 112, "name": "child1.2", "children": [
                 {"id": 1121, "name": "child1.2.1", "children": []},
                 {"id": 1123, "name": "child1.2.3", "statusComment": "ok", "children": []}]}]},
         {"id": 12, "name": "child2", "children": []}]},
    {"id": 2, "name": "parent2", "statusComment": "tekshirilgan",
     "contact": {"phone": "+998 93 555 44 33", "address": {"city": "Samarqand"}},
     "children": [
         {"id": 21, "name": "child2.1", "children": [
             {"id": 211, "name": "child2.1.1", "children": []}]}]},
]
ORDERS = [
    {"name": "Ali", "orders": [{"oid": 7, "sum": 1}, {"oid": 8}]},
    {"name": "Vali"},
    {"name": "G", "orders": []},
    {"name": "N", "orders": None},
    {"name": "X", "orders": [{"sum": 2}, {"oid": None}]},
]


# ----------------------------------------------------------------------------------------
# fixtures and helpers
# ----------------------------------------------------------------------------------------
@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(10)
    ctx.errors = []
    ctx.statuses = []
    ctx.error_reported.connect(ctx.errors.append)
    ctx.status_message.connect(lambda text, _ms: ctx.statuses.append(text))
    yield ctx
    for w in list(widgets._kept_alive):
        w.close()
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def opened(qtbot, app):
    made: list[DocSession] = []

    def factory(path: Path, **kw: Any) -> DocSession:
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
def views(qtbot):
    made: list[MapView] = []

    def make(session: DocSession, *, width: int = 1300, height: int = 760, wait: bool = True) -> MapView:
        view = MapView(session)
        qtbot.addWidget(view)
        view.resize(width, height)
        view.show()
        qtbot.waitExposed(view)
        view.activate()
        made.append(view)
        if wait:
            qtbot.waitUntil(lambda: view.state.designer is not None, timeout=TIMEOUT)
            settle(qtbot, view)
        return view

    yield make
    for v in made:
        v.shutdown()


def write_json(path: Path, value: Any, *, indent: int | None = None) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=indent), encoding="utf-8")
    return path


def settle(qtbot: Any, view: MapView) -> None:
    """Wait until no preview is pending or running."""
    qtbot.waitUntil(lambda: not view._preview_timer.isActive() and view._preview_task is None
                    and not view._preview_dirty, timeout=TIMEOUT)


def preview_now(qtbot: Any, view: MapView) -> str:
    view.schedule_preview(now=True)
    settle(qtbot, view)
    return view.preview_text()


def node(view: MapView, ref: Any) -> Any:
    n = view.state.tree.find(ref)
    assert n is not None, ref
    return n


def row_of(view: MapView, ref: Any, *, select: bool = True) -> int:
    """The row of ``ref`` (its ancestors opened); ``select``: it becomes the current row."""
    n = node(view, ref)
    r = view.model.row_of(n.id)
    if r is None or select:
        before = view._current_id
        assert view.select_node(n.id), ref
        if not select and before is not None:
            view.select_node(before)
        r = view.model.row_of(n.id)
    assert r is not None
    return r


def info(view: MapView, ref: Any) -> mapmodel.RowInfo:
    return view.model.info(row_of(view, ref, select=False))


def at(view: MapView, row: int, frac: float = 0.5, x: int | None = None) -> QPoint:
    """A point of ``row`` (scrolled into view) at ``frac`` of its height; ``x`` default: on its name."""
    view.tree.scrollTo(view.model.index(row, 0))
    QApplication.processEvents()
    rect = view.tree.row_rect(row)
    assert rect.isValid() and rect.height() > 0, row
    if x is None:
        label = view.tree.label_rect(row)
        x = label.left() + max(2, min(12, label.width() // 2))
    return QPoint(x, rect.top() + int(frac * rect.height()))


def outline(tree: Any) -> list[tuple]:
    """Structure without ids / UI fields: (depth, key, kind, src, flags)."""
    out = []
    for n, _p, d in tree.walk():
        out.append((d, n.key, n.kind, None if n.src is None else tuple(n.src), n.is_elem, n.is_root, n.synthetic,
                    n.computed, n.rec_key))
    return out


def gui_parses(monkeypatch: Any) -> list[int]:
    """Sizes of the values parsed by ``codec.loads`` in the GUI (main) thread."""
    main = threading.main_thread()
    seen: list[int] = []
    real = codec.loads

    def loads(data: Any) -> Any:
        if threading.current_thread() is main:
            seen.append(len(data))
        return real(data)

    monkeypatch.setattr(codec, "loads", loads)
    return seen


# ----------------------------------------------------------------------------------------
# building the tree: every document kind
# ----------------------------------------------------------------------------------------
KIND_RECORDS = [{"id": i, "name": f"n{i}", "tags": ["a", i], "contact": {"city": "Toshkent" if i % 2 else "Тошкент"}}
                for i in range(6)]


SINGLE = {"title": "bitta", "items": [1, 2, 3], "inner": {"a": 1, "b": [{"c": 1}]}}


def _write_kind(tmp_path: Path, kind: str) -> tuple[Path, dict]:
    recs = KIND_RECORDS
    if kind == "array":
        return write_json(tmp_path / "a.json", recs), {}
    if kind == "jsonl":
        p = tmp_path / "a.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
        return p, {}
    if kind == "stream":
        p = tmp_path / "a.txt"
        p.write_text("\n".join(json.dumps(r, indent=1) for r in recs), encoding="utf-8")
        return p, {"format": "stream"}
    if kind == "brace_list":
        p = tmp_path / "b.json"
        p.write_text("{" + ",\n".join(json.dumps(r) for r in recs) + "}", encoding="utf-8")
        return p, {}
    if kind == "wrapped":
        return write_json(tmp_path / "w.json", {"meta": {"v": 1, "tags": ["x"]}, "data": recs, "tail": True}), {}
    if kind == "keyed":
        return write_json(tmp_path / "k.json", {f"id{i}": r for i, r in enumerate(recs)}), {}
    if kind == "single":
        return write_json(tmp_path / "s.json", SINGLE), {}
    raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["array", "jsonl", "stream", "brace_list", "wrapped", "keyed", "single"])
def test_tree_is_built_for_every_document_kind(qtbot, opened, views, tmp_path, kind, monkeypatch) -> None:
    parses = gui_parses(monkeypatch)
    path, kw = _write_kind(tmp_path, kind)
    s = opened(path, **kw)
    assert s.doc.kind == kind
    v = views(s)
    st = v.state
    tree = st.tree
    doc = s.doc.pinned()
    values = [doc.record(i) for i in range(doc.record_count)]
    if kind == "wrapped":
        wrapper = {"meta": {"v": 1, "tags": ["x"]}, "data": [], "tail": True}
        want = mp.infer_tree(values, root_kind="wrapped", wrapper=wrapper, record_path=("data",))
    elif kind == "keyed":
        want = mp.infer_tree(values, root_kind="keyed")
    elif kind == "single":
        want = mp.infer_tree(values, root_kind="value")
    else:
        want = mp.infer_tree(values, root_kind="records")
    assert outline(tree) == outline(want)
    assert tree.root_kind == mp.root_kind_for(kind)
    # rows: the root label and meta, the element labels
    root = v.model.info(0)
    if kind == "keyed":
        assert root.label == "har bir a'zo" and ("6 ta a'zo", "") in root.meta
    else:
        assert root.label == s.name
    if kind in mapmodel.RECORD_KINDS:
        assert ("6 ta yozuv", "") in root.meta
        assert info(v, "[]").label == "har bir yozuv"
        assert info(v, "[].tags[]").label == "har bir element"
    if kind == "wrapped":
        assert info(v, "data[]").label == "har bir yozuv"          # the record array of the wrapper
        assert info(v, "meta.tags[]").label == "har bir element"
    assert v.stats_label.text() == mp.stats_text(tree, 0)
    assert "Namunadan: " in v.source_label.text() or kind == "single"
    # the preview: the first record through the identity mapping
    text = v.preview_text()
    if kind in mapmodel.RECORD_KINDS:
        assert json.loads(text) == KIND_RECORDS[0]
    elif kind == "wrapped":
        assert json.loads(text) == {"meta": {"v": 1, "tags": ["x"]}, "data": [KIND_RECORDS[0]], "tail": True}
    elif kind == "keyed":
        assert json.loads(text) == {"id0": KIND_RECORDS[0]}
    else:
        assert json.loads(text) == SINGLE
    assert v.preview.nav.isVisible() == (kind != "single")
    assert not [n for n in parses if n > 1 << 20]
    assert not s.app.errors


def test_rows_look_like_the_prototype(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "namuna.json", SAMPLE))
    v = views(s)
    rows = v.model.rows()
    texts = []
    for i, r in enumerate(rows):
        inf = v.model.info(i)
        chips = "".join(c[0] for c in inf.chips)
        meta = " ".join(m[0] for m in inf.meta)
        mark = ("v" if inf.open else ">") if inf.has_children else " "
        texts.append(f"{'  ' * r.depth}{mark} {inf.label} {chips} {meta} ~ {inf.sample}".rstrip(" ~"))
    assert texts == [
        "v namuna.json [ ] 2 ta yozuv",
        "  v har bir yozuv { } 2 ta",
        "      id 123  ~ 1, 2",
        "      name abc  ~ \"parent1\", \"parent2\"",
        "      statusComment abcnull  ~ \"tekshirilgan\"",
        "    v contact { }",
        "        phone abc  ~ \"+998 90 123 45 67\", \"+998 93 555 44 33\"",
        "      v address { }",
        "          city abc  ~ \"Toshkent\", \"Samarqand\"",
        "          street abc 50% ~ \"Navoiy 12\"",
        "    v children [ ]",
        "      v har bir element { } 3 ta",
        "          id 123  ~ 11, 12, 21",
        "          name abc  ~ \"child1\", \"child2\", \"child2.1\"",
        "          statusComment null 33%",
        "        v children [ ]",
        "          v har bir element { } 2 ta",
        "              id 123  ~ 112, 211",
        "              name abc  ~ \"child1.2\", \"child2.1.1\"",
        "            > children [ ]",
    ]
    assert v.stats_label.text() == "20 maydon"
    # tooltips of chips and meta, row actions of the prototype
    street = info(v, "[].contact.address.street")
    assert street.meta == [("50%", "1 / 2 ta obyektda bor")]
    assert street.chips == [("abc", "matn", "string")]
    assert info(v, "").actions == ["flatten"]                         # the root array can be flattened (A15 2b)
    assert info(v, "[]").actions == ["group"]                         # the record element (only array elem)
    assert info(v, "[].contact").actions == ["group", "unwrap", "rename", "delete"]
    assert info(v, "[].children").actions == ["flatten", "rename", "delete"]
    assert info(v, "[].id").actions == ["rename", "delete"]
    assert info(v, "[].children[]").draggable is False and info(v, "[].id").draggable


def test_more_rows_show_400_children_at_a_time(qtbot, opened, views, tmp_path) -> None:
    record = {f"k{i:03d}": i for i in range(950)}
    s = opened(write_json(tmp_path / "wide.json", [record]))
    v = views(s)
    rows = v.model.rows()
    assert len(rows) == 2 + 400 + 1
    more = rows[-1]
    assert more.kind == "more" and more.hidden == 550
    assert v.model.info(len(rows) - 1).more_text == "yana 550 ta maydonni ko'rsatish"
    # a click on the "more" row shows 400 more
    QTest.mouseClick(v.tree.viewport(), LEFT, M.NoModifier, at(v, len(rows) - 1))
    qtbot.waitUntil(lambda: len(v.model.rows()) == 2 + 800 + 1, timeout=TIMEOUT)
    assert v.model.rows()[-1].hidden == 150
    v.tree.setFocus()
    v._select_row(len(v.model.rows()) - 1)
    QTest.keyClick(v.tree, K.Key_Return)
    qtbot.waitUntil(lambda: len(v.model.rows()) == 2 + 950, timeout=TIMEOUT)


# ----------------------------------------------------------------------------------------
# operations: buttons, keys, context menu
# ----------------------------------------------------------------------------------------
def hover(qtbot: Any, view: MapView, row: int) -> None:
    QTest.mouseMove(view.tree.viewport(), at(view, row, x=40))
    qtbot.waitUntil(lambda: view.tree.hover_row == row, timeout=TIMEOUT)


def click_action(qtbot: Any, view: MapView, ref: Any, action: str) -> None:
    row = row_of(view, ref)
    hover(qtbot, view, row)
    rect = view.tree.action_rect(row, action)
    assert rect is not None, (ref, action)
    assert action in view.model.info(row).actions
    QTest.mouseClick(view.tree.viewport(), LEFT, M.NoModifier, rect.center())


def type_name(qtbot: Any, view: MapView, text: str, *, key: K = K.Key_Return) -> None:
    qtbot.waitUntil(lambda: isinstance(QApplication.focusWidget(), QLineEdit), timeout=TIMEOUT)
    editor = QApplication.focusWidget()
    assert editor.objectName() == "mapview_rename"
    editor.selectAll()
    QTest.keyClicks(editor, text)
    QTest.keyClick(editor, key)


def test_group_rename_move_via_buttons_and_keys(qtbot, opened, views, tmp_path) -> None:
    # AGENT_PROMPT test 7: new group, statusComment into it, the group named "holat"
    data = SAMPLE + [{"id": 3, "name": "parent3"}]
    s = opened(write_json(tmp_path / "namuna.json", data))
    v = views(s)
    v.tree.setFocus()
    click_action(qtbot, v, "[]", "group")
    group = node(v, "[].yangi_guruh")
    assert group.synthetic and v.model.info(v.model.row_of(group.id)).badges[0][:1] == ("yangi guruh",)
    type_name(qtbot, v, "holat")
    qtbot.waitUntil(lambda: v.state.tree.find("[].holat") is not None, timeout=TIMEOUT)
    assert v.state.designer.changes == 2                         # add + rename: two steps (the prototype's)
    # Ctrl+X on statusComment, Ctrl+V on the group: inside
    row_of(v, "[].statusComment")
    v.tree.setFocus()
    QTest.keyClick(v.tree, K.Key_X, M.ControlModifier)
    assert v.state.cut_id == node(v, "[].statusComment").id
    assert "kesib olindi" in v.toast_text()
    row_of(v, "[].holat")
    QTest.keyClick(v.tree, K.Key_V, M.ControlModifier)
    moved = node(v, "[].holat.statusComment")
    assert v.state.cut_id is None
    badges = [b[0] for b in info(v, "[].holat.statusComment").badges]
    assert badges == ["ko'chirildi"]
    assert info(v, "[].holat").badges[0][0] == "yangi guruh"
    assert v.current_row().node_id == moved.id
    out = [json.loads(line) for line in [preview_now(qtbot, v)]]
    assert out[0]["holat"] == {"statusComment": None}
    v.set_record(2)
    settle(qtbot, v)
    assert json.loads(v.preview_text()) == {"id": 3, "name": "parent3", "holat": {}}      # a group is always written
    assert v.stats_label.text() == "21 maydon · 3 o'zgarish"            # the group is a field too


def test_rename_unwrap_delete_flatten_and_toasts(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "namuna.json", SAMPLE))
    v = views(s)
    v.tree.setFocus()
    # F2: inline rename; the original name becomes a badge
    row_of(v, "[].name")
    QTest.keyClick(v.tree, K.Key_F2)
    type_name(qtbot, v, "ism")
    qtbot.waitUntil(lambda: v.state.tree.find("[].ism") is not None, timeout=TIMEOUT)
    assert ("asl nomi: name", "", "muted") in info(v, "[].ism").badges
    # Esc in the editor: nothing changes; an unchanged / empty name changes nothing either
    row_of(v, "[].ism")
    QTest.keyClick(v.tree, K.Key_F2)
    type_name(qtbot, v, "boshqa", key=K.Key_Escape)
    assert v.state.tree.find("[].ism") is not None and v.state.designer.changes == 1
    assert v.commit_rename(node(v, "[].ism").id, "  ism ") is None
    assert v.commit_rename(node(v, "[].ism").id, "   ") is None
    # a taken name: "_2" and the prototype's toast
    res = v.commit_rename(node(v, "[].ism").id, "id")
    assert res.applied and node(v, "[].id_2") is not None
    assert v.toast_text() == "«id» band edi, «id_2» deb nomlandi"
    assert s.app.statuses[-1] == v.toast_text()
    # Ctrl+Shift+U: unwrap contact (AGENT_PROMPT test 4)
    row_of(v, "[].contact")
    QTest.keyClick(v.tree, K.Key_U, M.ControlModifier | M.ShiftModifier)
    assert v.toast_text() == "«contact» ichidagi 2 ta maydon bir pog'ona yuqoriga chiqdi"
    assert v.state.tree.find("[].contact") is None
    assert [b[0] for b in info(v, "[].phone").badges] == ["ko'chirildi"]
    out = json.loads(preview_now(qtbot, v))
    assert out["phone"] == "+998 90 123 45 67" and out["address"] == {"city": "Toshkent", "street": "Navoiy 12"}
    # Del: delete statusComment
    row_of(v, "[].statusComment")
    QTest.keyClick(v.tree, K.Key_Delete)
    assert v.toast_text() == "«statusComment» natijadan olib tashlandi"
    assert "statusComment" not in json.loads(preview_now(qtbot, v))
    # flatten children (AGENT_PROMPT test 2): the row button, a task, then the toast
    click_action(qtbot, v, "[].children", "flatten")
    qtbot.waitUntil(lambda: node(v, "[].children[]").rec_key == "children", timeout=TIMEOUT)
    qtbot.waitUntil(lambda: not v.state.busy, timeout=TIMEOUT)
    assert v.toast_text() == "«children» ichidagi barcha darajalar bitta massivga yig'ildi: 7 ta element"
    elem = info(v, "[].children[]")
    assert ("rekursiv · children", "«children» ichidagi barcha darajalar shu massivga tushadi", "ok") in elem.badges
    depth = info(v, "[].children[]._depth")
    assert depth.badges[0][0] == "daraja raqami" and depth.sample == "1, 2, 3 …" and depth.chips[0][0] == "123"
    out = json.loads(preview_now(qtbot, v))
    assert [(c["name"], c["_depth"]) for c in out["children"]] == [
        ("child1", 1), ("child1.2", 2), ("child1.2.1", 3), ("child1.2.3", 3), ("child2", 1)]
    assert all("children" not in c for c in out["children"])
    # undo / redo / reset through the toolbar actions
    steps = v.state.designer.changes
    v.act_undo.trigger()
    assert v.state.designer.changes == steps - 1 and v.toast_text() == "Oxirgi o'zgarish bekor qilindi"
    assert node(v, "[].children[]").rec_key is None
    assert v.act_redo.isEnabled()
    v.act_redo.trigger()
    assert node(v, "[].children[]").rec_key == "children"
    v.act_reset.trigger()
    assert v.toast_text() == "Asl tuzilma tiklandi"
    assert outline(v.state.tree) == outline(v.state.designer.orig)
    assert v.state.designer.can_undo                              # reset is undoable
    v.act_undo.trigger()
    assert node(v, "[].children[]").rec_key == "children"
    assert not s.app.errors


def test_keyboard_moves_and_expansion(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "namuna.json", SAMPLE))
    v = views(s)
    v.tree.setFocus()
    keys = lambda: [c.key for c in node(v, "[]").children]   # noqa: E731
    assert keys() == ["id", "name", "statusComment", "contact", "children"]
    row_of(v, "[].name")
    QTest.keyClick(v.tree, K.Key_Up, M.AltModifier)
    assert keys() == ["name", "id", "statusComment", "contact", "children"]
    assert v.current_row().node.key == "name"
    QTest.keyClick(v.tree, K.Key_Up, M.AltModifier)
    assert v.toast_text() == "Bu qator allaqachon birinchi."
    QTest.keyClick(v.tree, K.Key_Down, M.AltModifier)
    QTest.keyClick(v.tree, K.Key_Down, M.AltModifier)
    assert keys() == ["id", "statusComment", "name", "contact", "children"]
    # Left / Right / Enter: collapse, parent, expand, first child
    row_of(v, "[].contact")
    QTest.keyClick(v.tree, K.Key_Left)
    assert not node(v, "[].contact").open
    QTest.keyClick(v.tree, K.Key_Left)
    assert v.current_row().node.is_elem
    row_of(v, "[].contact")
    QTest.keyClick(v.tree, K.Key_Right)
    assert node(v, "[].contact").open
    QTest.keyClick(v.tree, K.Key_Right)
    assert v.current_row().node.key == "phone"
    row_of(v, "[].contact")
    QTest.keyClick(v.tree, K.Key_Return)
    assert not node(v, "[].contact").open
    # Insert: a new group in the current object + the inline editor (Esc keeps the default name)
    row_of(v, "[].contact")
    QTest.keyClick(v.tree, K.Key_Insert)
    type_name(qtbot, v, "x", key=K.Key_Escape)
    assert node(v, "[].contact.yangi_guruh").synthetic and node(v, "[].contact").open
    # Insert on a value row: refused with the reason
    row_of(v, "[].id")
    QTest.keyClick(v.tree, K.Key_Insert)
    assert v.toast_text() == mp.t("mapping.why.not_object")
    # Ctrl+X on the root / an element row: refused; Esc forgets a cut
    v._select_row(0)
    QTest.keyClick(v.tree, K.Key_X, M.ControlModifier)
    assert v.state.cut_id is None and v.toast_text() == mp.t("mapping.why.root")
    row_of(v, "[].id")
    QTest.keyClick(v.tree, K.Key_X, M.ControlModifier)
    assert v.state.cut_id is not None
    QTest.keyClick(v.tree, K.Key_Escape)
    assert v.state.cut_id is None and v.toast_text() == "Ko'chirish bekor qilindi."
    # Ctrl+V without a cut: the reason; into its own subtree: refused
    QTest.keyClick(v.tree, K.Key_V, M.ControlModifier)
    assert v.toast_text() == "Avval ko'chiriladigan tugunni Ctrl+X bilan kesib oling."
    row_of(v, "[].contact")
    QTest.keyClick(v.tree, K.Key_X, M.ControlModifier)
    row_of(v, "[].contact.address")
    QTest.keyClick(v.tree, K.Key_V, M.ControlModifier)
    assert v.toast_text() == mp.t("mapping.why.into_itself")
    # paste on a value row: after it (the drop zone of the middle of the row)
    row_of(v, "[].id")
    QTest.keyClick(v.tree, K.Key_V, M.ControlModifier)
    assert keys()[:2] == ["id", "contact"]
    # the window-less fallbacks: Ctrl+Z / Ctrl+Y / Ctrl+Shift+Z
    steps = v.state.designer.changes
    QTest.keyClick(v.tree, K.Key_Z, M.ControlModifier)
    assert v.state.designer.changes == steps - 1 and keys()[:2] == ["id", "statusComment"]
    QTest.keyClick(v.tree, K.Key_Y, M.ControlModifier)
    assert keys()[:2] == ["id", "contact"]
    QTest.keyClick(v.tree, K.Key_Z, M.ControlModifier)
    QTest.keyClick(v.tree, K.Key_Z, M.ControlModifier | M.ShiftModifier)
    assert keys()[:2] == ["id", "contact"]
    assert not s.app.errors


def test_context_menu_lists_every_operation_with_reasons(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "namuna.json", SAMPLE))
    v = views(s)
    row_of(v, "[].id")
    menu = v.build_menu()
    acts = {a.objectName(): a for a in menu.actions() if a.objectName()}
    assert list(acts) == ["mapview_group", "mapview_unwrap", "mapview_flatten", "mapview_rename", "mapview_delete",
                          "mapview_cut", "mapview_paste", "mapview_move_up", "mapview_move_down", "mapview_toggle",
                          "mapview_expand_all", "mapview_collapse_all", "mapview_copy_source"]
    assert not acts["mapview_group"].isEnabled() and acts["mapview_group"].toolTip() == mp.t("mapping.why.not_object")
    assert not acts["mapview_flatten"].isEnabled()
    assert acts["mapview_flatten"].toolTip() == mp.t("mapping.why.not_flattenable")
    assert acts["mapview_rename"].isEnabled() and acts["mapview_delete"].isEnabled()
    assert not acts["mapview_paste"].isEnabled() and "Ctrl+X" in acts["mapview_paste"].toolTip()
    assert not acts["mapview_move_up"].isEnabled() and acts["mapview_move_up"].toolTip() == "Bu qator allaqachon birinchi."
    assert not acts["mapview_toggle"].isEnabled()
    assert acts["mapview_rename"].text().endswith("\tF2")
    menu.deleteLater()
    # the record element: only "group" and the expansion
    row_of(v, "[]")
    menu = v.build_menu()
    acts = {a.objectName(): a for a in menu.actions() if a.objectName()}
    assert [k for k, a in acts.items() if a.isEnabled()] == ["mapview_group", "mapview_toggle", "mapview_expand_all",
                                                             "mapview_collapse_all", "mapview_copy_source"]
    assert acts["mapview_delete"].toolTip() == mp.t("mapping.why.last_elem")
    assert acts["mapview_cut"].toolTip() == mp.t("mapping.why.elem")
    menu.deleteLater()
    # triggering: delete, copy the source path
    row_of(v, "[].contact.address.city")
    menu = v.build_menu()
    acts = {a.objectName(): a for a in menu.actions() if a.objectName()}
    acts["mapview_copy_source"].trigger()
    assert QGuiApplication.clipboard().text() == "[].contact.address.city"
    acts["mapview_delete"].trigger()
    assert v.state.tree.find("[].contact.address.city") is None
    menu.deleteLater()
    # the context menu event opens it on the clicked row
    row = row_of(v, "[].name")
    seen = []
    real = v.build_menu

    def spy() -> Any:
        m = real()
        seen.append(m)
        return m

    v.build_menu = spy
    from PySide6.QtGui import QContextMenuEvent

    ev = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, at(v, row_of(v, "[].id")),
                           v.tree.viewport().mapToGlobal(at(v, row)))
    QApplication.sendEvent(v.tree.viewport(), ev)
    assert seen and v.current_row().node.key == "id"
    seen[0].close()
    del v.build_menu


# ----------------------------------------------------------------------------------------
# drag & drop with real mouse events
# ----------------------------------------------------------------------------------------
def press(view: MapView, pos: QPoint) -> None:
    QTest.mousePress(view.tree.viewport(), LEFT, M.NoModifier, pos)


def move(view: MapView, pos: QPoint) -> None:
    QTest.mouseMove(view.tree.viewport(), pos)


def release(view: MapView, pos: QPoint) -> None:
    QTest.mouseRelease(view.tree.viewport(), LEFT, M.NoModifier, pos)


def start_drag(view: MapView, ref: Any) -> None:
    row = row_of(view, ref)
    p = at(view, row, 0.5)
    press(view, p)
    move(view, p + QPoint(QApplication.startDragDistance() + 3, 0))
    assert view.tree.drag_active(), ref


def drop_on(view: MapView, ref: Any, frac: float) -> tuple[int, str] | None:
    """Move the running drag over ``ref`` at ``frac`` of its row; the drop target now."""
    n = node(view, ref)
    row = view.model.row_of(n.id)
    assert row is not None, ref
    move(view, at(view, row, frac))
    return view.tree.drop_target()


def test_drag_and_drop_zones_and_moves(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "namuna.json", SAMPLE))
    v = views(s)
    tree = v.tree
    d = v.state.designer
    # AGENT_PROMPT test 3: city before name (top 28 % of the name row)
    start_drag(v, "[].contact.address.city")
    name_row = v.model.row_of(node(v, "[].name").id)
    assert drop_on(v, "[].name", 0.1) == (name_row, "before")
    assert tree.viewport().cursor().shape() == Qt.CursorShape.DragMoveCursor
    assert drop_on(v, "[].name", 0.9) == (name_row, "after")
    assert drop_on(v, "[].name", 0.55) == (name_row, "after")             # a value row: no "inside" (>= 0.5: after)
    assert drop_on(v, "[].name", 0.4) == (name_row, "before")
    contact_row = v.model.row_of(node(v, "[].contact").id)
    assert drop_on(v, "[].contact", 0.15) == (contact_row, "before")      # top 28 %: before
    assert drop_on(v, "[].contact", 0.5) == (contact_row, "inside")       # an object row: inside in the middle
    assert drop_on(v, "[].contact", 0.88) == (contact_row, "after")       # bottom 28 %: after
    # the root of an array file: only arrays (merge into the records) -> forbidden for a value
    assert drop_on(v, "", 0.5) is None
    assert tree.viewport().cursor().shape() == Qt.CursorShape.ForbiddenCursor
    # an element row of scalars: forbidden
    assert drop_on(v, "[].name", 0.1) is not None
    release(v, at(v, name_row, 0.1))
    assert not tree.drag_active() and tree.viewport().cursor().shape() == Qt.CursorShape.ArrowCursor
    assert [c.key for c in node(v, "[]").children][:3] == ["id", "city", "name"]
    assert [b[0] for b in info(v, "[].city").badges] == ["ko'chirildi"]
    assert info(v, "[].city").badges[0][1] == "Asl joyi: [].contact.address.city"
    out = json.loads(preview_now(qtbot, v))
    assert list(out)[:3] == ["id", "city", "name"] and out["contact"]["address"] == {"street": "Navoiy 12"}
    # into its own subtree: forbidden; Esc cancels a drag
    start_drag(v, "[].contact")
    assert drop_on(v, "[].contact.address", 0.5) is None
    assert drop_on(v, "[].contact", 0.5) is None
    QTest.keyClick(tree, K.Key_Escape)
    assert not tree.drag_active() and tree.drop_target() is None
    release(v, at(v, row_of(v, "[].id"), 0.5))
    assert node(v, "[].contact") is not None and d.changes == 1
    # root and element rows cannot be dragged
    p = at(v, 0)
    press(v, p)
    move(v, p + QPoint(QApplication.startDragDistance() + 3, 0))
    assert not tree.drag_active()
    release(v, p)
    p = at(v, row_of(v, "[]"))
    press(v, p)
    move(v, p + QPoint(QApplication.startDragDistance() + 3, 0))
    assert not tree.drag_active()
    release(v, p)
    # AGENT_PROMPT test 8: children[].children dropped onto children merges the elements
    start_drag(v, "[].children[].children")
    assert drop_on(v, "[].name", 0.5) is not None                          # an array next to a value: allowed
    assert drop_on(v, "[].children", 0.5)[1] == "inside"
    release(v, at(v, v.model.row_of(node(v, "[].children").id), 0.5))
    assert v.toast_text() == "«children» elementlari «children» massiviga qo'shildi"
    elems = [c for c in node(v, "[].children").children]
    assert len(elems) == 2
    second = v.model.info(v.model.row_of(elems[1].id))
    assert ("manba: [].children[].children", "Bu elementlar boshqa massivdan qo'shilgan", "array") in second.badges
    out = json.loads(preview_now(qtbot, v))
    assert [c["id"] for c in out["children"]] == [11, 12, 112]
    assert d.changes == 2
    assert not s.app.errors


def test_drag_auto_scrolls_near_the_edges(qtbot, opened, views, tmp_path) -> None:
    record = {f"k{i:03d}": {"v": i} for i in range(120)}
    s = opened(write_json(tmp_path / "long.json", [record]))
    v = views(s, height=420)
    v.state.collapse_all()
    tree = v.tree
    bar = tree.verticalScrollBar()
    assert bar.maximum() > 0 and bar.value() == 0
    start_drag(v, "[].k001")
    vp = tree.viewport()
    move(v, QPoint(60, vp.height() - 2))
    qtbot.waitUntil(lambda: bar.value() > 0, timeout=TIMEOUT)
    move(v, QPoint(60, vp.height() // 2))
    QTest.keyClick(tree, K.Key_Escape)
    release(v, QPoint(60, vp.height() // 2))
    assert v.state.designer.changes == 0


def test_badges_gather_and_broadcast(qtbot, opened, views, tmp_path) -> None:
    # AGENT_PROMPT tests 5 and 6
    s = opened(write_json(tmp_path / "orders.json", ORDERS))
    v = views(s)
    st = v.state
    st.run("move", node(v, "[].orders[].oid").id, node(v, "[].name").id, "after")
    oid = info(v, "[].oid")
    assert [b[0] for b in oid.badges] == ["ko'chirildi", "ro'yxatga yig'iladi"]
    assert oid.badges[1][2] == "warn"
    assert oid.meta[0][0] == "75%"
    outs = []
    for i in range(len(ORDERS)):
        v.set_record(i)
        settle(qtbot, v)
        outs.append(json.loads(v.preview_text()))
    assert outs == [{"name": "Ali", "oid": [7, 8], "orders": [{"sum": 1}, {}]}, {"name": "Vali"},
                    {"name": "G", "oid": [], "orders": []}, {"name": "N", "oid": [], "orders": None},
                    {"name": "X", "oid": [None], "orders": [{"sum": 2}, {}]}]
    st.undo()
    st.run("move", node(v, "[].name").id, node(v, "[].orders[]").id, "inside")
    name = info(v, "[].orders[].name")
    assert [b[0] for b in name.badges] == ["ko'chirildi", "har elementga takrorlanadi"]
    assert name.badges[1][2] == "bool"
    v.set_record(0)
    settle(qtbot, v)
    assert json.loads(v.preview_text()) == {"orders": [{"oid": 7, "sum": 1, "name": "Ali"}, {"oid": 8, "name": "Ali"}]}


# ----------------------------------------------------------------------------------------
# undo / redo / reset, stats, buttons
# ----------------------------------------------------------------------------------------
def test_toolbar_states_undo_redo_reset(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "namuna.json", SAMPLE))
    v = views(s)
    assert not v.act_undo.isEnabled() and not v.act_redo.isEnabled() and not v.act_reset.isEnabled()
    assert v.act_save.isEnabled() and v.act_export.isEnabled() and v.act_full.isEnabled()
    assert not v.can_undo and not v.can_redo and v.undo_text == ""
    history = []
    v.history_changed.connect(lambda: history.append(1))
    v.state.run("delete", node(v, "[].id").id)
    assert history and v.can_undo and v.undo_text == "tuzilma"
    assert v.act_undo.isEnabled() and v.act_reset.isEnabled() and not v.act_redo.isEnabled()
    assert v.stats_label.text() == "19 maydon · 1 o'zgarish"
    assert v.undo() and v.stats_label.text() == "20 maydon" and v.can_redo
    assert v.redo() and v.stats_label.text() == "19 maydon · 1 o'zgarish"
    assert v.reset() and v.stats_label.text() == "20 maydon · 2 o'zgarish"
    # expand all / collapse (no undo step)
    v.act_collapse.trigger()
    assert [r.node.key for r in v.model.rows() if r.kind == "node" and not r.node.is_root and not r.node.is_elem] == \
        ["id", "name", "statusComment", "contact", "children"]
    v.act_expand.trigger()
    assert len(v.model.rows()) == 1 + sum(1 for _ in v.state.tree.iter_nodes()) - 1
    assert v.stats_label.text() == "20 maydon · 2 o'zgarish"
    # the caret toggles a row, the toggled state is part of the tree (undo restores it)
    row = row_of(v, "[].contact")
    caret = v.tree.caret_rect(row)
    QTest.mouseClick(v.tree.viewport(), LEFT, M.NoModifier, caret.center())
    assert not node(v, "[].contact").open
    QTest.mouseDClick(v.tree.viewport(), LEFT, M.NoModifier, at(v, row, x=caret.right() + 30))
    assert node(v, "[].contact").open


def test_expand_to_level_like_the_tree_view(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "namuna.json", SAMPLE))
    v = views(s)
    v._select_row(0)
    v.expand_to_level(2)                                              # the root + the record element
    shown = [r.node.key or ("[]" if r.node.is_elem else "(root)") for r in v.model.rows()]
    assert shown == ["(root)", "[]", "id", "name", "statusComment", "contact", "children"]
    assert v.state.designer.changes == 0                              # no undo step
    row_of(v, "[].children")
    v.expand_to_level(1)                                              # one level below the current row
    assert node(v, "[].children").open and not node(v, "[].children[]").open
    assert v.current_row().node.key == "children"
    v.expand_to_level(99)                                             # clamped to 9 levels
    assert all(n.open for n, _p, d in v.state.tree.walk() if d < 9 and n.src is not None
               and n.src[:2] == (None, "children"))


# ----------------------------------------------------------------------------------------
# preview
# ----------------------------------------------------------------------------------------
def test_preview_modes_navigation_and_limits(qtbot, opened, views, tmp_path) -> None:
    big = {"n": 1.10, "e": 1e400, "big": 12345678901234567890, "neg": -0.0, "list": list(range(30)),
           "long": "x" * 400, "wide": {f"k{i}": i for i in range(205)}, "s": "Oʻzbekiston 'Ўзбекистон'"}
    path = tmp_path / "p.json"
    text = json.dumps([{"i": 0}, big, {"i": 2}]).replace("1.1,", "1.10,").replace("Infinity", "1e400") \
        .replace("-0.0", "-0.0")
    path.write_text(text, encoding="utf-8")
    s = opened(path)
    v = views(s)
    assert v.preview.record_spin.value() == 0 and v.preview.total_label.text() == "/ 3 ta yozuv"
    assert not v.preview.prev_button.isEnabled() and v.preview.next_button.isEnabled()
    assert v.preview.foot.text() == "#0 yozuv natijada qanday chiqishi. Ko'rinishda massivlarning 20 tasigacha " \
                                    "elementi; eksportda hammasi chiqadi."
    v.preview.next_button.click()
    settle(qtbot, v)
    assert v.record_index() == 1 and v.preview.record_spin.value() == 1
    out = v.preview_text()
    assert '"n": 1.10' in out and '"e": 1e400' in out and '"big": 12345678901234567890' in out and \
        '"neg": -0.0' in out
    assert "    19,\n    … davomi eksportda\n  ]" in out                        # 20 items + the marker (limit 21)
    assert '"long": "' + "x" * 300 + '…"' in out
    assert "… yana 5 ta kalit" in out
    assert "Oʻzbekiston 'Ўзбекистон'" in out
    # Asl: the record unchanged ("… yana N ta")
    v.preview.src_button.click()
    settle(qtbot, v)
    src = v.preview_text()
    assert "… yana 10 ta" in src and "davomi" not in src
    assert v.preview.foot.text() == "Fayldagi #1 yozuv, o'zgartirilmagan holda."
    # the spin box: 0-based, clamped
    v.preview.record_spin.setValue(2)
    settle(qtbot, v)
    assert json.loads(v.preview_text()) == {"i": 2} and not v.preview.next_button.isEnabled()
    # following the session's current record (another view selected a node)
    s.select(NodeRef(0, ("i",)))
    settle(qtbot, v)
    assert v.record_index() == 0 and json.loads(v.preview_text()) == {"i": 0}
    # a designer change refreshes the preview (debounced)
    v.preview.out_button.click()
    settle(qtbot, v)
    v.state.run("rename", node(v, "[].i").id, "j")
    assert v._preview_timer.isActive()
    settle(qtbot, v)
    assert json.loads(v.preview_text()) == {"j": 0}


def test_record_navigator_holds_numbers_beyond_32_bits(qtbot) -> None:
    from PySide6.QtWidgets import QVBoxLayout, QWidget

    from jsonreshape.gui.mapview import RecordSpin

    host = QWidget()
    qtbot.addWidget(host)
    lay = QVBoxLayout(host)
    spin = RecordSpin(host)
    other = QLineEdit(host)
    lay.addWidget(spin)
    lay.addWidget(other)
    host.show()
    qtbot.waitExposed(host)
    seen: list[int] = []
    spin.valueChanged.connect(seen.append)
    count = 5_000_000_000                                # more records than a QSpinBox can hold
    spin.setRange(0, count - 1)
    spin.setValue(3_000_000_000)
    assert spin.value() == 3_000_000_000 and spin.lineEdit().text() == "#3000000000" and seen == [3_000_000_000]
    spin.stepBy(1)
    assert spin.value() == 3_000_000_001
    spin.setFocus()
    qtbot.waitUntil(spin.hasFocus, timeout=TIMEOUT)
    QTest.keyClick(spin, K.Key_Up)
    assert spin.value() == 3_000_000_002
    # typing: digits only ("#" allowed), Enter commits, numbers above the maximum are clamped
    spin.lineEdit().selectAll()
    QTest.keyClicks(spin.lineEdit(), "4999999998")
    QTest.keyClick(spin.lineEdit(), K.Key_Return)
    assert spin.value() == 4_999_999_998
    spin.lineEdit().selectAll()
    QTest.keyClicks(spin.lineEdit(), "abc")
    assert spin.lineEdit().text() == "#4999999998"
    spin.lineEdit().selectAll()
    QTest.keyClicks(spin.lineEdit(), "99999999999")
    QTest.keyClick(spin.lineEdit(), K.Key_Return)
    assert spin.value() == count - 1 and spin.lineEdit().text() == f"#{count - 1}"
    assert spin.stepEnabled() == QAbstractSpinBox.StepEnabledFlag.StepDownEnabled
    # leaving the field commits; an empty field keeps the value
    spin.lineEdit().selectAll()
    QTest.keyClicks(spin.lineEdit(), "#12")
    other.setFocus()
    qtbot.waitUntil(lambda: spin.value() == 12, timeout=TIMEOUT)
    spin.setFocus()
    spin.lineEdit().selectAll()
    QTest.keyClick(spin.lineEdit(), K.Key_Backspace)
    other.setFocus()
    qtbot.waitUntil(lambda: spin.lineEdit().text() == "#12", timeout=TIMEOUT)
    spin.setValue(0)
    assert spin.stepEnabled() == QAbstractSpinBox.StepEnabledFlag.StepUpEnabled
    spin.setRange(0, 10)
    assert spin.maximum() == 10 and spin.minimum() == 0
    assert RecordSpin.valueFromText(" # 7 ") == 7 and RecordSpin.valueFromText("٣") is None
    assert spin.sizeHint().width() >= spin.fontMetrics().horizontalAdvance("#9999")


def test_preview_pane_navigator_for_billions_of_records(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "a.json", [{"i": 0}, {"i": 1}]))
    v = views(s)
    pane = v.preview
    asked: list[int] = []
    pane.record_requested.disconnect(v.set_record)              # the pane alone (the view clamps to 2 records)
    pane.record_requested.connect(asked.append)
    pane.set_navigator(3_000_000_000, 5_000_000_000, visible=True, members=False)
    assert pane.record_spin.value() == 3_000_000_000 and pane.total_label.text() == "/ 5 000 000 000 ta yozuv"
    assert pane.prev_button.isEnabled() and pane.next_button.isEnabled()
    pane.next_button.click()
    pane.prev_button.click()
    pane.record_spin.setValue(4_999_999_999)
    assert asked == [3_000_000_001, 2_999_999_999, 4_999_999_999]
    pane.set_navigator(4_999_999_999, 5_000_000_000, visible=True, members=True)
    assert not pane.next_button.isEnabled() and pane.total_label.text() == "/ 5 000 000 000 ta a'zo"
    # the view clamps a request to its document's records
    pane.record_requested.connect(v.set_record)
    v.set_record(4_999_999_999)
    settle(qtbot, v)
    assert v.record_index() == 1 and json.loads(v.preview_text()) == {"i": 1}


def test_preview_refreshes_after_an_edit_and_reads_big_records_in_tasks(qtbot, opened, views, tmp_path,
                                                                         monkeypatch) -> None:
    parses = gui_parses(monkeypatch)
    big = {"id": 1, "blob": "z" * (2 << 20), "inner": {"a": 1}}
    s = opened(write_json(tmp_path / "e.json", [{"id": 0, "name": "x"}, big]))
    v = views(s)
    assert json.loads(v.preview_text()) == {"id": 0, "name": "x"}
    v.set_record(1)
    settle(qtbot, v)
    assert '"blob": "' + "z" * 300 + '…"' in v.preview_text()
    # an edit of the shown record: the preview follows (the tree stays)
    qtbot.waitUntil(lambda: s.edit is not None and s.edit.available, timeout=TIMEOUT)
    v.set_record(0)
    settle(qtbot, v)
    before = v.state.version
    got = []
    s.data_changed.connect(got.append)
    s.edit.set_value(NodeRef(0, ("name",)), '"yangi"')
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    settle(qtbot, v)
    assert json.loads(v.preview_text()) == {"id": 0, "name": "yangi"}
    assert v.state.version == before
    assert "Saqlanmagan o'zgarishlar ham hisobga olingan." in v.preview.foot.text()
    assert not [n for n in parses if n > 1 << 20]
    assert not s.app.errors


def test_preview_of_a_record_over_the_limit_is_not_read(qtbot, opened, views, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mapmodel, "PREVIEW_MAX_BYTES", 1000)
    s = opened(write_json(tmp_path / "e.json", [{"id": 0}, {"blob": "q" * 5000}]))
    v = views(s)
    v.set_record(1)
    settle(qtbot, v)
    note = v.preview.note()
    assert note.startswith("Ko'rinishni hisoblab bo'lmadi: #1 yozuv ko'rinish uchun juda katta")
    v.set_record(0)
    settle(qtbot, v)
    assert json.loads(v.preview_text()) == {"id": 0}


# ----------------------------------------------------------------------------------------
# mapping files
# ----------------------------------------------------------------------------------------
def test_save_and_open_mapping_round_trip(qtbot, opened, views, tmp_path, monkeypatch) -> None:
    s = opened(write_json(tmp_path / "namuna.json", SAMPLE))
    v = views(s)
    st = v.state
    st.run("move", node(v, "[].contact.address.city").id, node(v, "[].name").id, "before")
    st.run("rename", node(v, "[].city").id, "shahar")
    target = tmp_path / "out" / "map.json"
    target.parent.mkdir()
    asked = []
    v.ask_save_path = lambda title, default, filters: asked.append(default) or str(target)
    task = v.save_mapping()
    qtbot.waitUntil(lambda: task.state != "running", timeout=TIMEOUT)
    assert task.state == "done" and target.exists()
    assert asked == [str(tmp_path / "namuna.mapping.json")]
    assert v.toast_text() == f"Tuzilma saqlandi: {os.path.normpath(target)}"
    loaded, info_ = mp.load_mapping(target)
    assert outline(loaded) == outline(st.tree)
    assert info_["kind"] == "array" and info_["sample"] == 10000 and info_["full_scan"] is False
    saved_preview = preview_now(qtbot, v)
    # more changes, then open the saved mapping (asked first: the changes are lost)
    st.run("delete", node(v, "[].id").id)
    monkeypatch.setattr(widgets, "confirm_handler", lambda parent, text, title: True)
    v.ask_open_path = lambda title, folder, filters: str(target)
    v.open_mapping()
    qtbot.waitUntil(lambda: st.source == "file", timeout=TIMEOUT)
    assert outline(st.tree) == outline(loaded) and not st.designer.can_undo
    assert "Fayldan: map.json" in v.source_label.text()
    assert preview_now(qtbot, v) == saved_preview
    # "Asl holat" after opening a saved mapping: the original inferred tree (stored in the file) -- enabled
    # without undo steps because the loaded tree differs from it
    assert v.act_reset.isEnabled() and v.buttons["mapview_reset"].isEnabled() and st.loaded_edited
    v.act_reset.trigger()
    assert node(v, "[].contact.address.city") is not None and st.tree.find("[].shahar") is None
    assert v.toast_text() == "Asl tuzilma tiklandi" and st.designer.can_undo
    v.act_undo.trigger()
    assert st.tree.find("[].shahar") is not None and v.act_reset.isEnabled()
    v.act_redo.trigger()
    assert node(v, "[].contact.address.city") is not None
    # the "no" answer keeps the tree
    monkeypatch.setattr(widgets, "confirm_handler", lambda parent, text, title: False)
    opened_paths = []
    v.ask_open_path = lambda title, folder, filters: opened_paths.append(folder) or str(target)
    v.open_mapping()
    qtbot.wait(50)
    assert opened_paths == [] and node(v, "[].contact.address.city") is not None
    assert not s.app.errors
    # a mapping saved without changes: nothing to go back to after opening it
    s2 = opened(write_json(tmp_path / "other.json", ORDERS))
    v2 = views(s2)
    plain = tmp_path / "plain.mapping.json"
    task = v2.save_mapping(str(plain))
    qtbot.waitUntil(lambda: task.state != "running", timeout=TIMEOUT)
    v2.open_mapping(str(plain))
    qtbot.waitUntil(lambda: v2.state.source == "file", timeout=TIMEOUT)
    assert not v2.state.loaded_edited and not v2.act_reset.isEnabled()
    assert mapmodel.structure_of(v2.state.tree) == mapmodel.structure_of(v2.state.designer.orig)


def test_opening_a_mapping_for_another_kind_is_refused(qtbot, opened, views, tmp_path, app) -> None:
    keyed = opened(write_json(tmp_path / "k.json", {"a": {"x": 1}, "b": {"x": 2}}))
    kv = views(keyed)
    path = tmp_path / "keyed.mapping.json"
    task = kv.state.save_mapping(str(path))
    qtbot.waitUntil(lambda: task.state != "running", timeout=TIMEOUT)
    s = opened(write_json(tmp_path / "namuna.json", SAMPLE))
    v = views(s)
    before = outline(v.state.tree)
    v.open_mapping(str(path))
    qtbot.waitUntil(lambda: bool(app.errors), timeout=TIMEOUT)
    err = app.errors[-1]
    assert getattr(err, "key", "") == "engine.error.kind_mismatch"
    assert outline(v.state.tree) == before and v.state.source == "sample"
    app.errors.clear()
    # a broken file: the mapping error is reported, nothing changes
    bad = tmp_path / "bad.json"
    bad.write_text('{"version": 1, "tree": {"kind": "array", "children": []}}', encoding="utf-8")
    v.open_mapping(str(bad))
    qtbot.waitUntil(lambda: bool(app.errors), timeout=TIMEOUT)
    assert app.errors[-1].key.startswith("mapping.error.")
    assert outline(v.state.tree) == before
    app.errors.clear()


# ----------------------------------------------------------------------------------------
# full scan, reopen, one designer per session
# ----------------------------------------------------------------------------------------
def test_full_scan_finds_fields_the_sample_missed(qtbot, opened, views, tmp_path, app, monkeypatch) -> None:
    app.settings.sample_size = 3
    recs = [{"id": i} for i in range(9)]
    recs[5]["rare"] = "faqat bitta"
    s = opened(write_json(tmp_path / "r.json", recs))
    v = views(s)
    assert v.state.tree.find("[].rare") is None
    assert v.source_label.text() == "Namunadan: 3 / 9 yozuv"
    v.state.run("rename", node(v, "[].id").id, "nomer")
    monkeypatch.setattr(widgets, "confirm_handler", lambda parent, text, title: False)
    v.act_full.trigger()
    qtbot.wait(50)
    assert v.state.source == "sample" and not v.state.building
    monkeypatch.setattr(widgets, "confirm_handler", lambda parent, text, title: True)
    seen = []
    app.tasks.task_added.connect(seen.append)
    v.act_full.trigger()
    qtbot.waitUntil(lambda: v.state.source == "full", timeout=TIMEOUT)
    assert [t.kind for t in seen] == ["mapping"] and seen[0].heavy
    assert v.state.tree.find("[].rare") is not None and not v.state.designer.can_undo
    assert v.source_label.text() == "Barcha 9 yozuvdan"


def test_one_designer_per_session_and_reopen_rules(qtbot, opened, views, tmp_path, app) -> None:
    path = write_json(tmp_path / "w.json", {"meta": {"v": 1}, "data": [{"id": 1}, {"id": 2}, {"id": 3}],
                                             "other": [{"k": 1}, {"k": 2}]})
    s = opened(path)
    v1 = views(s)
    v2 = views(s)
    assert v1.state is v2.state
    v1.state.run("rename", node(v1, "data[].id").id, "nomer")
    assert v2.state.tree.find("data[].nomer") is not None
    assert v2.stats_label.text() == v1.stats_label.text()
    # a reopen with the same kind / record path keeps an edited designer
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen(discard_edits=True)
    qtbot.waitUntil(lambda: s.state == "ready", timeout=TIMEOUT)
    assert v1.state.tree.find("data[].nomer") is not None and v1.state.designer.can_undo
    settle(qtbot, v1)
    assert json.loads(v1.preview_text())["data"] == [{"nomer": 1}]
    # another record path: the tree is inferred anew (with a message)
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen(record_path="other", discard_edits=True)
    qtbot.waitUntil(lambda: v1.state.designer is not None and v1.state.tree.find("other[].k") is not None
                    and v1.state.tree.root_kind == "wrapped" and v1.state.record_path() == ("other",)
                    and not v1.state.designer.can_undo, timeout=TIMEOUT)
    assert v1.state.tree.find("data[].nomer") is None
    assert any("qaytadan aniqlandi" in x or "yangidan aniqlandi" in x for x in app.statuses)
    settle(qtbot, v1)
    assert json.loads(v1.preview_text())["other"] == [{"k": 1}]


def test_refusals_for_unsupported_documents(qtbot, opened, views, tmp_path) -> None:
    # a wrapped document whose records are object members: the engine refuses it, so does the builder
    path = write_json(tmp_path / "m.json", {"meta": 1, "items": {"a": {"x": 1}, "b": {"x": 2}}})
    s = opened(path, record_path="items")
    if s.doc.kind != "wrapped" or not s.doc.member_records:
        pytest.skip("the reader opened this file differently")
    v = views(s, wait=False)
    qtbot.waitUntil(lambda: v.state.phase == "failed", timeout=TIMEOUT)
    assert v.stack.currentIndex() == 0 and v.retry_button.isVisible()
    assert "obyektning a'zolari" in v.message_label.text()
    assert not v.act_export.isEnabled()
    assert v.export_with_mapping() is None and v.toast_text() == "Tuzilma hali tayyor emas."


# ----------------------------------------------------------------------------------------
# more: GUI-thread rule, busy flatten, errors, painting, keyed navigation
# ----------------------------------------------------------------------------------------
def test_no_data_is_parsed_or_read_in_bulk_on_the_gui_thread(qtbot, opened, views, tmp_path, monkeypatch) -> None:
    from views_edit_helpers import GuiReads

    parses = gui_parses(monkeypatch)
    big = {"id": 1, "blob": "b" * (1536 * 1024), "children": [{"id": 2, "children": [{"id": 3}]}]}
    path = write_json(tmp_path / "big.json", [{"id": 0, "children": []}, big, {"id": 9}])
    reads = GuiReads(monkeypatch)
    s = opened(path)
    v = views(s)
    v.set_record(1)
    settle(qtbot, v)
    assert '"blob": "' + "b" * 300 + '…"' in v.preview_text()
    v.preview.src_button.click()
    settle(qtbot, v)
    v.preview.out_button.click()
    settle(qtbot, v)
    v.run_action("flatten", node(v, "[].children").id)
    qtbot.waitUntil(lambda: not v.state.busy and node(v, "[].children[]").rec_key == "children", timeout=TIMEOUT)
    from jsonreshape.gui.exportdialog import ExportDialog, MappingSource

    dlg = ExportDialog(s, v, mapping=MappingSource(tree=v.state.tree_copy))
    qtbot.addWidget(dlg)
    dlg.set_output_path(str(tmp_path / "out.json"))
    with qtbot.waitSignal(dlg.export_finished, timeout=TIMEOUT):
        assert dlg.start_export() is not None
    assert not [n for n in parses if n > 1 << 20], parses
    assert reads.big() == [], reads.reads
    assert not s.app.errors


def test_operations_wait_while_a_flatten_is_computed(qtbot, opened, views, tmp_path, monkeypatch) -> None:
    s = opened(write_json(tmp_path / "namuna.json", SAMPLE))
    v = views(s)
    gate = threading.Event()
    real = mp.Designer.plan_flatten

    def slow(self: Any, *args: Any, **kw: Any) -> Any:
        assert gate.wait(20)
        return real(self, *args, **kw)

    monkeypatch.setattr(mp.Designer, "plan_flatten", slow)
    v.run_action("flatten", node(v, "[].children").id)
    assert v.state.busy and "tekislanmoqda" in v.source_label.text()
    assert not v.act_undo.isEnabled() and not v.can_undo
    assert v.state.run("delete", node(v, "[].id").id) is None
    assert v.toast_text() == "Rekursiv tekislash hisoblanmoqda: tugashini kuting."
    row_of(v, "[].name")
    v.tree.setFocus()
    QTest.keyClick(v.tree, K.Key_F2)
    assert not isinstance(QApplication.focusWidget(), QLineEdit)
    menu = v.build_menu()
    acts = {a.objectName(): a for a in menu.actions() if a.objectName()}
    assert not acts["mapview_delete"].isEnabled() and acts["mapview_delete"].toolTip() == v.toast_text()
    assert acts["mapview_expand_all"].isEnabled() and acts["mapview_collapse_all"].isEnabled()
    menu.deleteLater()
    # expanding / collapsing stays possible
    v.state.toggle(node(v, "[].contact").id)
    assert not node(v, "[].contact").open
    gate.set()
    qtbot.waitUntil(lambda: not v.state.busy, timeout=TIMEOUT)
    assert node(v, "[].children[]").rec_key == "children" and v.state.designer.changes == 1
    assert node(v, "[].id") is not None


def test_mapping_file_errors_are_reported(qtbot, opened, views, tmp_path, app) -> None:
    s = opened(write_json(tmp_path / "namuna.json", SAMPLE))
    v = views(s)
    task = v.save_mapping(str(tmp_path / "missing" / "dir" / "m.json"))
    qtbot.waitUntil(lambda: task.state != "running", timeout=TIMEOUT)
    assert app.errors and v.state.file_path is None
    app.errors.clear()
    # no extension: ".json" is added
    task = v.save_mapping(str(tmp_path / "plain"))
    qtbot.waitUntil(lambda: task.state != "running", timeout=TIMEOUT)
    assert (tmp_path / "plain.json").exists() and v.state.file_path == str(tmp_path / "plain.json")
    assert v._default_mapping_path() == str(tmp_path / "plain.json")
    # cancelled dialogs do nothing
    v.ask_save_path = lambda *_a: None
    assert v.save_mapping() is None
    v.ask_open_path = lambda *_a: None
    v.open_mapping()
    assert v.state.source == "sample"


def test_reopen_of_an_unedited_designer_infers_it_again(qtbot, opened, views, tmp_path) -> None:
    path = write_json(tmp_path / "a.json", [{"id": 1}, {"id": 2}])
    s = opened(path)
    v = views(s)
    first = v.state.designer
    path.write_text(json.dumps([{"id": 1, "new": True}, {"id": 2}]), encoding="utf-8")
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen(discard_edits=True)
    qtbot.waitUntil(lambda: v.state.designer is not None and v.state.designer is not first, timeout=TIMEOUT)
    assert v.state.tree.find("[].new") is not None and v.state.source == "sample"
    settle(qtbot, v)
    assert json.loads(v.preview_text()) == {"id": 1, "new": True}


def test_deep_rows_keep_their_names_on_screen(qtbot, opened, views, tmp_path) -> None:
    from jsonreshape.gui.mapview import MapDelegate

    value: dict = {"leaf": "qiymat"}
    for i in range(45):
        value = {"name": f"n{i}", "children": [value]}
    s = opened(write_json(tmp_path / "deep.json", [value]))
    v = views(s, width=900)
    v.state.expand_all()
    assert max(r.depth for r in v.model.rows()) == 92
    width = v.tree.viewport().width()
    xs = [MapDelegate.indent(d, width) for d in range(130)]
    assert xs[:5] == [0, 18, 36, 54, 72]                              # the prototype's 18 px per level first
    assert all(a <= b for a, b in zip(xs, xs[1:])) and xs[-1] <= int(width * 0.6)
    leaf = row_of(v, "[]." + "children[]." * 45 + "leaf")
    label = v.tree.label_rect(leaf)
    assert label.width() > 20 and label.right() < width
    assert v.tree.part_at(label.center())[:2] == (leaf, "label")
    # a drag onto a deep row still finds its zones
    start_drag(v, "[].name")
    assert drop_on(v, "[]." + "children[]." * 44 + "name", 0.2) is not None
    QTest.keyClick(v.tree, K.Key_Escape)
    release(v, at(v, leaf))
    assert not v.grab().isNull()


def test_reopen_keeps_a_full_scan_tree(qtbot, opened, views, tmp_path, app) -> None:
    app.settings.sample_size = 3
    recs: list[dict] = [{"id": i} for i in range(9)]
    recs[5]["rare"] = "faqat bitta"
    path = write_json(tmp_path / "r.json", recs)
    s = opened(path)
    v = views(s)
    assert v.state.tree.find("[].rare") is None
    v.act_full.trigger()                                             # no changes: no question
    qtbot.waitUntil(lambda: v.state.source == "full", timeout=TIMEOUT)
    full = v.state.designer
    assert v.state.tree.find("[].rare") is not None and v.state.designer.undo_limit == app.settings.undo_limit
    # the file changes and is reopened: the full-scan tree stays (a sample tree would lose "rare")
    recs.append({"id": 9, "late": True})
    path.write_text(json.dumps(recs), encoding="utf-8")
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen(discard_edits=True)
    qtbot.waitUntil(lambda: s.state == "ready", timeout=TIMEOUT)
    settle(qtbot, v)
    assert v.state.designer is full and v.state.source == "full" and v.state.tree.find("[].rare") is not None
    assert v.state.tree.find("[].late") is None and v.source_label.text() == "Barcha 9 yozuvdan"
    v.set_record(9)
    settle(qtbot, v)
    assert json.loads(v.preview_text()) == {"id": 9}                 # the preview reads the new document
    # "To'liq skanerlash" again reads the new document
    v.act_full.trigger()
    qtbot.waitUntil(lambda: v.state.designer is not full and v.state.source == "full", timeout=TIMEOUT)
    assert v.state.tree.find("[].late") is not None and v.source_label.text() == "Barcha 10 yozuvdan"
    assert not app.errors


def test_painting_hover_actions_drag_indicator_and_zoom(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "namuna.json", SAMPLE))
    v = views(s)
    v.state.run("move", node(v, "[].contact.address.city").id, node(v, "[].name").id, "after")
    row = row_of(v, "[].city")
    hover(qtbot, v, row)
    assert v.tree.action_rect(row, "rename") is not None and v.tree.action_rect(row, "group") is None
    image = v.grab()
    assert not image.isNull()
    # tooltips of the parts (a badge, a chip, an action)
    parts = v.tree.delegate.layout(v.model.row(row), v.model.info(row), v.tree.row_rect(row), with_actions=True)
    badge_rect, badge_text, badge_tip, _role = parts.badges[0]
    assert badge_text == "ko'chirildi" and badge_tip == "Asl joyi: [].contact.address.city"
    assert v.tree.part_at(badge_rect.center())[1:] == ("badge", "Asl joyi: [].contact.address.city")
    assert v.tree.part_at(parts.chips[0][0].center())[1:] == ("chip", "matn")
    assert v.tree.part_at(parts.actions[0][0].center())[1] == "action"
    # a drag in progress paints its indicator
    start_drag(v, "[].id")
    drop_on(v, "[].contact", 0.5)
    assert not v.grab().isNull()
    QTest.keyClick(v.tree, K.Key_Escape)
    release(v, at(v, row_of(v, "[].name")))
    # zoom: fonts and row height follow
    h = v.tree.delegate.row_h
    v.zoom(4)
    assert v.tree.delegate.row_h > h and v.preview.edit.font().pointSizeF() > 0
    v.zoom(0)
    assert v.tree.delegate.row_h == h
    # copy commands of the window (Ctrl+C / Ctrl+Shift+C): the source path; the key
    row_of(v, "[].city")
    assert v.copy_value() == "[].contact.address.city" and v.copy_path() == "[].contact.address.city"
    assert v.copy_key() == "city" and v.current_ref() is None
    v._select_row(0)
    assert v.copy_key() is None


def test_keyed_document_navigation(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "k.json", {"child1": {"id": 1, "x": {"y": 1}}, "child2": {"id": 2}}))
    assert s.doc.kind == "keyed"
    v = views(s)
    assert v.preview.total_label.text() == "/ 2 ta a'zo"
    assert v.preview.foot.text().startswith("#0 a'zo natijada qanday chiqishi.")
    v.preview.next_button.click()
    settle(qtbot, v)
    assert json.loads(v.preview_text()) == {"child2": {"id": 2}}
    v.preview.src_button.click()
    settle(qtbot, v)
    assert json.loads(v.preview_text()) == {"child2": {"id": 2}}
    assert v.preview.foot.text() == "Fayldagi #1 a'zo, o'zgartirilmagan holda."
    s.select(NodeRef(0, ("x",)))
    settle(qtbot, v)
    assert json.loads(v.preview_text()) == {"child1": {"id": 1, "x": {"y": 1}}}



# ----------------------------------------------------------------------------------------
# review findings (stage 4): races, replaced trees, the inline rename, selection, menus, texts
# ----------------------------------------------------------------------------------------
def test_preview_follows_the_current_record_while_a_preview_runs(qtbot, opened, views, tmp_path,
                                                                  monkeypatch) -> None:
    import time

    from jsonreshape.gui import mapview as mapview_mod

    s = opened(write_json(tmp_path / "a.json", [{"i": i} for i in range(5)]))
    v = views(s)
    real = mapview_mod.compute_preview

    def slow(*args: Any, **kw: Any) -> Any:
        time.sleep(0.03)                                              # a record that takes a while to read
        return real(*args, **kw)

    monkeypatch.setattr(mapview_mod, "compute_preview", slow)
    v.set_record(1)                                                   # a preview of record 1 is running ...
    s.select(NodeRef(3, ("i",)))                                      # ... when another view selects record 3
    settle(qtbot, v)
    qtbot.wait(100)
    settle(qtbot, v)
    assert v.record_index() == 3 and json.loads(v.preview_text()) == {"i": 3}
    assert v.preview.record_spin.value() == 3 and v.preview.foot.text().startswith("#3 yozuv")


def test_operations_wait_while_a_full_scan_runs(qtbot, opened, views, tmp_path, app, monkeypatch) -> None:
    app.settings.sample_size = 3
    recs: list[dict] = [{"id": i, "name": f"n{i}"} for i in range(9)]
    recs[5]["rare"] = True
    s = opened(write_json(tmp_path / "r.json", recs))
    v = views(s)
    gate = threading.Event()
    real = mapmodel.build_tree

    def gated(*args: Any, **kw: Any) -> Any:
        if kw.get("full"):
            assert gate.wait(20)
        return real(*args, **kw)

    monkeypatch.setattr(mapmodel, "build_tree", gated)
    v.act_full.trigger()                                              # no changes: no question
    st = v.state
    assert st.full_running and st.busy and not st.flattening
    # a change now would be replaced by the scanned tree: refused with the reason
    assert st.run("rename", node(v, "[].name").id, "ism") is None
    assert v.toast_text() == mp.t("gui.mapview.why.scanning")
    assert not v.act_undo.isEnabled() and not v.act_full.isEnabled() and not v.can_undo
    assert v.act_full.toolTip().endswith(mp.t("gui.mapview.why.scanning"))
    assert "tekislanmoqda" not in v.source_label.text() and "Skanerlanmoqda" in v.source_label.text()
    row_of(v, "[].name")
    v.tree.setFocus()
    QTest.keyClick(v.tree, K.Key_F2)
    assert not v.renaming()
    gate.set()
    qtbot.waitUntil(lambda: st.source == "full", timeout=TIMEOUT)
    assert st.tree.find("[].rare") is not None and st.tree.find("[].name") is not None
    assert not st.busy and v.act_full.isEnabled()
    assert st.run("rename", node(v, "[].name").id, "ism").applied


def test_saved_input_info_describes_the_tree(qtbot, opened, views, tmp_path, app, monkeypatch) -> None:
    monkeypatch.setattr(widgets, "confirm_handler", lambda parent, text, title: True)
    path = write_json(tmp_path / "w.json", {"meta": {"v": 1}, "data": [{"id": 1}, {"id": 2}], "other": [{"k": 1}]})
    s = opened(path, record_path="data")
    v = views(s)
    v.state.run("rename", node(v, "data[].id").id, "nomer")
    # a save while the document is being opened again describes the tree (not the document of the moment)
    s.reopen(discard_edits=True)
    target = tmp_path / "while_reopening.mapping.json"
    task = v.state.save_mapping(str(target))
    qtbot.waitUntil(lambda: task.state != "running", timeout=TIMEOUT)
    qtbot.waitUntil(lambda: s.state == "ready", timeout=TIMEOUT)
    saved = json.loads(target.read_text(encoding="utf-8"))["input"]
    assert saved["record_path"] == ["data"] and saved["kind"] == "wrapped" and saved["sample"] == 10000
    assert saved["source"] == str(path) and saved["full_scan"] is False
    # a mapping made for another record path keeps its own input when saved again
    other = opened(path, record_path="other")
    vo = views(other)
    made_for_other = tmp_path / "other.mapping.json"
    task = vo.state.save_mapping(str(made_for_other))
    qtbot.waitUntil(lambda: task.state != "running", timeout=TIMEOUT)
    v.open_mapping(str(made_for_other))                               # accepted with the engine's warning
    qtbot.waitUntil(lambda: v.state.source == "file", timeout=TIMEOUT)
    again = tmp_path / "again.mapping.json"
    task = v.state.save_mapping(str(again))
    qtbot.waitUntil(lambda: task.state != "running", timeout=TIMEOUT)
    info = json.loads(again.read_text(encoding="utf-8"))["input"]
    assert info["record_path"] == ["other"]
    tree, _info = mp.load_mapping(again)
    assert tree.record_src == ("other",)
    assert not app.errors


def test_big_preview_records_are_not_kept(qtbot, opened, views, tmp_path) -> None:
    big = {"id": 1, "blob": "q" * (mapmodel.VALUE_KEEP_MAX + 10)}
    s = opened(write_json(tmp_path / "b.json", [{"id": 0}, big]))
    v = views(s)
    assert v._value_cache is not None and v._value_cache[1] == {"id": 0}       # a small record is kept
    v.set_record(1)
    settle(qtbot, v)
    assert '"blob": "' in v.preview_text()
    assert v._value_cache is None and not hasattr(v, "_preview_result")      # the big one is read again


def test_one_parsed_wrapper_is_shared_by_the_views(qtbot, opened, views, tmp_path, monkeypatch) -> None:
    path = write_json(tmp_path / "w.json", {"meta": {"v": 1}, "data": [{"id": 1}, {"id": 2}], "tail": True})
    s = opened(path)
    calls: list[int] = []
    real = mapmodel.parse_wrapper

    def counting(*args: Any, **kw: Any) -> Any:
        calls.append(1)
        return real(*args, **kw)

    monkeypatch.setattr(mapmodel, "parse_wrapper", counting)
    v1 = views(s)
    v2 = views(s)
    preview_now(qtbot, v1)
    preview_now(qtbot, v2)
    st = v1.state
    assert st.wrapper_cache is not None and st.wrapper_cache[1] is st.designer.wrapper
    assert calls == []                                                # the build's parse serves every preview
    assert not hasattr(v1, "_wrapper_cache") and not hasattr(v2, "_wrapper_cache")


def test_a_drag_released_off_the_rows_moves_nothing(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "namuna.json", SAMPLE))
    v = views(s)
    tree = v.tree
    st = v.state
    before = outline(st.tree)
    name_row = row_of(v, "[].name")
    # released to the right of the tree (over the preview pane), at the height of a row
    start_drag(v, "[].id")
    p = at(v, name_row, 0.9)
    outside = QPoint(tree.viewport().width() + 250, p.y())
    move(v, outside)
    assert tree.drop_target() is None and tree.viewport().cursor().shape() == Qt.CursorShape.ForbiddenCursor
    release(v, outside)
    assert outline(st.tree) == before and st.designer.changes == 0
    # released in the empty space below the last row
    st.collapse_all()
    start_drag(v, "[].id")
    last = tree.row_rect(v.model.rowCount() - 1)
    below = QPoint(60, last.bottom() + 40)
    assert below.y() < tree.viewport().height() and not tree.indexAt(below).isValid()
    move(v, below)
    assert tree.drop_target() is None
    release(v, below)
    # above the view (while it would scroll): no target either
    start_drag(v, "[].id")
    move(v, QPoint(60, -5))
    assert tree.drop_target() is None
    QTest.keyClick(tree, K.Key_Escape)
    release(v, QPoint(60, -5))
    assert st.designer.changes == 0 and [c.key for c in node(v, "[]").children][0] == "id"


def test_a_new_tree_drops_menus_drags_and_renames_of_the_old_one(qtbot, opened, views, tmp_path, app) -> None:
    app.settings.sample_size = 3
    recs = [{"id": i, "contact": {"phone": f"p{i}"}, "name": f"n{i}", "city": "c"} for i in range(9)]
    recs[5]["contact"]["email"] = "only-here@x"                        # a field the sample misses
    s = opened(write_json(tmp_path / "r.json", recs))
    v = views(s)
    st = v.state
    # a context menu of [].name open while a Full scan replaces the tree (the new tree may reuse the id):
    # closed; the actions of a menu built for the old tree do nothing
    from PySide6.QtWidgets import QMenu

    row_of(v, "[].name")
    v.popup_menu(v.tree.viewport().mapToGlobal(QPoint(40, 40)))
    built = v.build_menu()
    acts = {a.objectName(): a for a in built.actions() if a.objectName()}
    st.full_scan()
    qtbot.waitUntil(lambda: st.source == "full", timeout=TIMEOUT)
    assert st.tree.find("[].contact.email") is not None
    assert v._menu is None                                            # closed with the old tree
    qtbot.waitUntil(lambda: v.findChildren(QMenu) == [built], timeout=TIMEOUT)
    acts["mapview_delete"].trigger()
    assert st.tree.find("[].name") is not None and st.tree.find("[].contact.email") is not None
    assert not st.designer.can_undo
    built.deleteLater()
    # a drag of the old tree: cancelled when the tree is replaced
    start_drag(v, "[].name")
    st.full_scan()
    qtbot.waitUntil(lambda: not st.full_running, timeout=TIMEOUT)
    assert not v.tree.drag_active()
    release(v, at(v, row_of(v, "[].city")))
    # a rename being typed: dropped, also its late commit
    row_of(v, "[].city")
    v.tree.setFocus()
    QTest.keyClick(v.tree, K.Key_F2)
    qtbot.waitUntil(v.renaming, timeout=TIMEOUT)
    v.tree.delegate.editing[0].setText("shahar")         # the rename editor itself (focus is racy under load)
    old = st.designer
    st.full_scan()
    qtbot.waitUntil(lambda: st.designer is not old, timeout=TIMEOUT)
    assert not v.renaming() and st.tree.find("[].shahar") is None
    assert v.commit_rename(node(v, "[].city").id, "shahar", designer=old) is None
    assert not st.designer.can_undo and not app.errors


def test_a_pending_rename_is_committed_before_toolbar_commands(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "namuna.json", SAMPLE))
    v = views(s)
    st = v.state
    st.run("delete", node(v, "[].statusComment").id)                  # an earlier step
    row_of(v, "[].name")
    v.tree.setFocus()
    QTest.keyClick(v.tree, K.Key_F2)
    qtbot.waitUntil(v.renaming, timeout=TIMEOUT)
    QTest.keyClicks(v.tree.delegate.editing[0], "ism")
    QTest.mouseClick(v.buttons["mapview_expand"], LEFT)               # a tool button does not take the focus
    assert not v.renaming() and st.tree.find("[].ism") is not None and st.designer.changes == 2
    row_of(v, "[].ism")
    v.tree.setFocus()
    QTest.keyClick(v.tree, K.Key_F2)
    qtbot.waitUntil(v.renaming, timeout=TIMEOUT)
    QTest.keyClicks(v.tree.delegate.editing[0], "boshqa")
    QTest.mouseClick(v.buttons["mapview_undo"], LEFT)                 # the rename, then its own undo
    qtbot.wait(20)
    assert st.tree.find("[].ism") is not None and st.tree.find("[].boshqa") is None
    assert st.tree.find("[].statusComment") is None and st.designer.changes == 2 and st.designer.can_redo
    v.redo()
    assert st.tree.find("[].boshqa") is not None


def test_selection_moves_to_the_visible_ancestor_when_its_row_hides(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "namuna.json", SAMPLE))
    v = views(s)
    row_of(v, "[].contact.address.city")
    v.act_collapse.trigger()
    assert v.current_row().node.key == "contact"
    v.tree.setFocus()
    QTest.keyClick(v.tree, K.Key_Delete)                              # acts on the visible ancestor, not a neighbour
    assert v.state.tree.find("[].contact") is None and v.state.tree.find("[].children") is not None


def test_unwrap_selects_the_first_lifted_field(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "namuna.json", SAMPLE))
    v = views(s)
    row_of(v, "[].contact")
    v.tree.setFocus()
    QTest.keyClick(v.tree, K.Key_U, M.ControlModifier | M.ShiftModifier)
    assert v.current_row().node.key == "phone"
    empty = opened(write_json(tmp_path / "e.json", [{"a": {}, "b": 1}]))
    ve = views(empty)
    row_of(ve, "[].a")
    assert ve.run_action("unwrap").applied and ve.current_row().node.is_elem       # nothing lifted: the parent


def test_a_drag_does_not_survive_hiding_the_view(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "namuna.json", SAMPLE))
    v = views(s)
    start_drag(v, "[].id")
    v.hide()                                                          # another view was shown in the pane
    v.show()
    qtbot.waitExposed(v)
    assert not v.tree.drag_active() and v.tree.viewport().cursor().shape() == Qt.CursorShape.ArrowCursor
    release(v, at(v, row_of(v, "[].name")))
    row_of(v, "[].name")
    v.tree.setFocus()
    QTest.keyClick(v.tree, K.Key_Delete)
    assert v.state.tree.find("[].name") is None
    # a drag whose button was released elsewhere (the tree never saw it) does not swallow the keys
    start_drag(v, "[].id")
    assert v.tree.drag_active()
    QTest.mouseRelease(v.preview.edit.viewport(), LEFT, M.NoModifier, QPoint(5, 5))
    assert v.tree.drag_active() and v.tree.drag_is_stale()
    v.tree.setFocus()
    QTest.keyClick(v.tree, K.Key_Delete)
    assert not v.tree.drag_active() and v.state.tree.find("[].id") is None


def test_context_menus_are_deleted_when_closed(qtbot, opened, views, tmp_path) -> None:
    from PySide6.QtGui import QContextMenuEvent
    from PySide6.QtWidgets import QMenu

    s = opened(write_json(tmp_path / "namuna.json", SAMPLE))
    v = views(s)
    row_of(v, "[].name")
    for _ in range(4):
        menu = v.popup_menu(v.tree.viewport().mapToGlobal(QPoint(30, 30)))
        assert menu is not None and v._menu is menu
        menu.close()
    qtbot.waitUntil(lambda: not v.findChildren(QMenu), timeout=TIMEOUT)
    assert v._menu is None
    # a right click on the empty space below the rows: no menu (not the one of a row off screen)
    v.state.collapse_all()
    last = v.tree.row_rect(v.model.rowCount() - 1)
    pos = QPoint(60, last.bottom() + 60)
    assert not v.tree.indexAt(pos).isValid()
    QApplication.sendEvent(v.tree.viewport(), QContextMenuEvent(QContextMenuEvent.Reason.Mouse, pos,
                                                                 v.tree.viewport().mapToGlobal(pos)))
    assert v._menu is None and not v.findChildren(QMenu)


def test_the_record_count_follows_edits(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "a.json", [{"id": i} for i in range(5)]))
    v = views(s)
    assert ("5 ta yozuv", "") in v.model.info(0).meta
    qtbot.waitUntil(lambda: s.edit is not None and s.edit.available, timeout=TIMEOUT)
    got: list = []
    s.data_changed.connect(got.append)
    s.edit.delete_records([0, 1])
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    settle(qtbot, v)
    assert ("3 ta yozuv", "") in v.model.info(0).meta
    assert v.preview.total_label.text() == "/ 3 ta yozuv"
    assert v.state.designer.changes == 0                              # the tree stays


def test_tooltips_and_toasts_show_data_as_text(qtbot, opened, views, tmp_path) -> None:
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QHelpEvent
    from PySide6.QtWidgets import QToolTip

    from jsonreshape.gui.mapview import plain_tip

    assert plain_tip("<b>qalin</b> & a\nb") == ("<p style='white-space:pre-wrap'>&lt;b&gt;qalin&lt;/b&gt; &amp; "
                                                "a\\u000ab</p>")
    recs = [{"note": "<b>qalin</b>", "a‮b": 1, "x": 2}, {"note": "<img src='x.png'> y", "x": 3}]
    s = opened(write_json(tmp_path / "t.json", recs))
    v = views(s, width=700)
    row = row_of(v, "[].note")
    parts = v.tree.delegate.layout(v.model.row(row), v.model.info(row), v.tree.row_rect(row), with_actions=False)
    assert parts.sample_full.startswith('"<b>qalin</b>"')
    chip = parts.chips[0][0].center()
    QApplication.sendEvent(v.tree.viewport(), QHelpEvent(QEvent.Type.ToolTip, chip,
                                                         v.tree.viewport().mapToGlobal(chip)))
    assert QToolTip.text().startswith("<p style='white-space:pre-wrap'>")
    QToolTip.hideText()
    # a toast naming a key with a bidi control: escaped (the rows escape it too)
    key = next(n for n in node(v, "[]").children if n.key.startswith("a"))
    v.state.run("delete", key.id)
    assert "\\u202e" in v.toast_text() and "‮" not in v.toast_text()
    assert "‮" not in s.app.statuses[-1]


def test_compact_is_shared_by_the_views_of_a_session(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "a.json", [{"id": 1}]))
    v1 = views(s)
    v2 = views(s)
    v1.compact_check.setChecked(True)
    assert v2.compact_check.isChecked() and v1.state.compact
    v2.compact_check.setChecked(False)
    assert not v1.compact_check.isChecked() and not v1.state.compact


def test_hover_ends_when_the_pointer_leaves_the_rows(qtbot, opened, views, tmp_path) -> None:
    from PySide6.QtCore import QEvent

    record = {f"k{i:03d}": i for i in range(80)}
    s = opened(write_json(tmp_path / "long.json", [record]))
    v = views(s, height=400)
    tree = v.tree
    hover(qtbot, v, 5)
    assert tree.shows_actions(5)
    QApplication.sendEvent(tree.viewport(), QEvent(QEvent.Type.Leave))     # e.g. onto the scroll bar
    assert tree.hover_row == -1 and not tree.shows_actions(5)
