"""Session restore model (jsonreshape/gui/sessionstore.py; docs/ARCHITECTURE.md §15.0 "Session restore"):
the tab / session JSON form and its round trip through the GUI state, strict validation with JSON pointers
(workspace files) and lenient reading (the GUI state: bad parts dropped), the size budget, the
``restore_session`` setting, the restore plan (missing files, ``relpath`` first, the active tab), relative
paths, and the duck-typed capture from a window (current node, expanded nodes, filters / sort, search inputs;
search results keep no filter). Qt-free: no window here (tests/test_gui_session_restore.py has them)."""

from __future__ import annotations

import json
import os
from collections import namedtuple
from decimal import Decimal
from pathlib import Path

import pytest

from jsonreshape.gui import sessionstore as ss
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.sessionstore import FormatProblem, SessionState, TabState
from jsonreshape.paths import format_path, parse_location
from jsonreshape.sortkeys import SortKey

Ref = namedtuple("Ref", "record path ipath", defaults=((), None))

UZ_NAME = "Oʻzbekiston ma'lumotlari — Тошкент"
KEY_TRICKY = 'ko\'cha "Amir Temur"'


def loc(record, *path) -> str:
    """``DocSession.location_text`` of ``NodeRef(record, path)``."""
    text = format_path(tuple(path))
    if record is None:
        return text or "$"
    if not text:
        return f"#{record}"
    return f"#{record}" + (text if text.startswith("[") else "." + text)


def full_tab(path: str = "C:\\data\\x.json") -> TabState:
    return TabState(
        path=path, format="jsonl", record_path=["data", 0, "items"], views=["table", "raw"], active_pane=1,
        second_pane=False, current=loc(12, "contact", KEY_TRICKY),
        expanded=["#0", "#12", "#12.contact", "#12.tags[3]", "$", loc(None, "meta", "Тошкент", "ko'cha")],
        filter={"text": "price == 1.10 and city == 'Toshkent'", "lang": "expr",
                "builder": {"op": "and", "negate": False, "items": [{"field": "price", "op": "eq", "value": 1.1,
                                                                      "value2": None}]}},
        quick_filters={"name": "~^Ўз", "contact.zip": ">= 100"},
        sort=[ss.sort_key_dict(SortKey("name", descending=True, nulls="first", natural=False, collation="simple",
                                       case_sensitive=True)), ss.sort_key_dict(SortKey("$index"))],
        search={"text": "G'ulom", "mode": "plain", "case_sensitive": False, "whole_word": True, "translit": True,
                "scope": "values", "path": "contact.*"},
        relpath="x.json", table_layout={"version": 2, "columns": [{"path": "id", "width": 80, "hidden": False}]},
        bookmarks=[{"name": UZ_NAME, "record": 12, "path": ["contact", 0], "created": 1.5},
                   {"name": "yangi", "record": {"rid": 7, "layer": "k" * 32}, "path": [], "created": 2.0}],
        mapping={"version": 1, "tree": {}}, edits="pending")


def through_json(data: dict) -> dict:
    return json.loads(json.dumps(data, ensure_ascii=False))


# ----------------------------------------------------------------------------------------
# the JSON form
# ----------------------------------------------------------------------------------------
def test_tab_round_trip_keeps_every_field() -> None:
    tab = full_tab()
    again = TabState.from_dict(through_json(tab.to_dict(workspace=True)), strict=True, workspace=True)
    assert again == tab
    # the location texts are what DocSession.location_text writes / parse_location reads
    assert parse_location(again.current).path == ("contact", KEY_TRICKY)


def test_session_form_has_no_workspace_extras() -> None:
    data = full_tab().to_dict()
    assert not {"relpath", "table_layout", "bookmarks", "mapping", "edits"} & set(data)
    tab = TabState.from_dict(through_json(data))
    assert tab.bookmarks == [] and tab.mapping is None and tab.edits == "none" and tab.relpath is None
    assert tab.expanded == full_tab().expanded and tab.search == full_tab().search
    # workspace keys in a session object are ignored unless asked for
    extra = dict(data, bookmarks=[{"name": "x", "record": 1}], mapping={"a": 1})
    assert TabState.from_dict(extra).bookmarks == []


def test_defaults_and_panes() -> None:
    tab = TabState.from_dict({"path": "a.json"})
    assert tab.panes() == ss.DEFAULT_VIEWS and tab.views == [None, None] and tab.second_pane and tab.active_pane == 0
    assert TabState(path="a", views=["table", None]).panes() == ("table", "text")
    assert TabState.from_dict({"path": "a", "views": ["schema"]}).views == ["schema", None]
    assert TabState(path=os.path.join("d", "Тошкент.json")).name == "Тошкент.json"


def test_session_state_through_the_gui_state(tmp_path: Path) -> None:
    state = GuiState.load(tmp_path / "gui_state.json")
    session = SessionState(tabs=[full_tab(str(tmp_path / "a.json")), TabState(path=str(tmp_path / "b.jsonl"))],
                           active=1)
    assert ss.store_session(state, session)
    assert state.save(raise_errors=True)
    stored = GuiState.load(tmp_path / "gui_state.json").get(ss.SESSION_KEY)
    assert stored["version"] == ss.SESSION_VERSION and stored["active"] == 1 and isinstance(stored["saved"], str)
    loaded = ss.load_session(GuiState.load(tmp_path / "gui_state.json"))
    assert loaded is not None and loaded.active == 1
    first = full_tab(str(tmp_path / "a.json"))
    for name in ("relpath", "table_layout", "bookmarks", "mapping", "edits"):
        setattr(first, name, getattr(TabState(path=""), name))
    assert loaded.tabs == [first, TabState(path=str(tmp_path / "b.jsonl"))]
    # nothing changed (only the time would): not stored again
    assert not ss.store_session(state, session)
    session.tabs[1].current = "#0"
    assert ss.store_session(state, session)


def test_load_session_without_or_with_an_unusable_session(tmp_path: Path) -> None:
    state = GuiState.load(tmp_path / "s.json")
    assert ss.load_session(state) is None
    state.set(ss.SESSION_KEY, {"version": 99, "tabs": []})
    assert ss.load_session(state) is None
    state.set(ss.SESSION_KEY, {"version": 1, "tabs": "no"})
    assert ss.load_session(state) is None
    state.set(ss.SESSION_KEY, [1, 2])
    assert ss.load_session(state) is None


# ----------------------------------------------------------------------------------------
# lenient (GUI state) and strict (workspace) reading
# ----------------------------------------------------------------------------------------
def test_lenient_reading_drops_bad_parts_and_tabs() -> None:
    data = {"version": 1, "active": 2, "tabs": [
        {"path": "a.json", "expanded": ["#0", "#x bad", 12, "#3.a"], "sort": [{"path": ""}], "views": "tree",
         "filter": {"text": "x > 1", "lang": "sql"}, "current": "#1", "active_pane": 5, "search": {"text": 3}},
        {"format": "jsonl"},                                   # no path: left out
        {"path": "c.json", "quick_filters": {"a": "== 1", "b": "  "}, "second_pane": "yes"},
        {"path": "   "},                                       # blank path: left out
    ]}
    session = SessionState.from_dict(data)
    assert [t_.path for t_ in session.tabs] == ["a.json", "c.json"]
    a, c = session.tabs
    assert a.expanded == ["#0", "#3.a"] and a.sort == [] and a.views == [None, None]
    assert a.filter is None and a.current == "#1" and a.active_pane == 0 and a.search is None
    assert c.quick_filters == {"a": "== 1"} and c.second_pane is True
    assert session.active == 1                                 # the saved active tab (index 2) is now index 1


def test_lenient_reading_caps_tabs_and_expanded() -> None:
    tabs = [{"path": f"f{i}.json", "expanded": [f"#{n}" for n in range(ss.EXPANDED_MAX + 5)]}
            for i in range(ss.TABS_MAX + 3)]
    session = SessionState.from_dict({"version": 1, "tabs": tabs})
    assert len(session.tabs) == ss.TABS_MAX
    assert len(session.tabs[0].expanded) == ss.EXPANDED_MAX


@pytest.mark.parametrize("data, code, pointer", [
    ([1], "type", "/x"),
    ({"format": "jsonl"}, "missing", "/x/path"),
    ({"path": 5}, "type", "/x/path"),
    ({"path": "  "}, "value", "/x/path"),
    ({"path": "a", "format": "JSON L"}, "value", "/x/format"),
    ({"path": "a", "record_path": {"a": 1}}, "type", "/x/record_path"),
    ({"path": "a", "record_path": ["data", -1]}, "value", "/x/record_path/1"),
    ({"path": "a", "record_path": "data[["}, "value", "/x/record_path"),
    ({"path": "a", "views": ["tree", "Text"]}, "value", "/x/views/1"),
    ({"path": "a", "views": ["tree", "text", "raw"]}, "too_many", "/x/views"),
    ({"path": "a", "active_pane": True}, "type", "/x/active_pane"),
    ({"path": "a", "active_pane": 2}, "value", "/x/active_pane"),
    ({"path": "a", "second_pane": 1}, "type", "/x/second_pane"),
    ({"path": "a", "current": "#12."}, "value", "/x/current"),
    ({"path": "a", "expanded": "#1"}, "type", "/x/expanded"),
    ({"path": "a", "expanded": ["#1", "#2", ""]}, "value", "/x/expanded/2"),
    ({"path": "a", "expanded": ["#1"] * (ss.EXPANDED_MAX + 1)}, "too_many", "/x/expanded"),
    ({"path": "a", "filter": "x > 1"}, "type", "/x/filter"),
    ({"path": "a", "filter": {"text": "x", "lang": "sql"}}, "value", "/x/filter/lang"),
    ({"path": "a", "filter": {"text": "x", "builder": []}}, "type", "/x/filter/builder"),
    ({"path": "a", "quick_filters": {"a/b": 1}}, "type", "/x/quick_filters/a~1b"),
    ({"path": "a", "sort": [{"path": "name"}, "id"]}, "type", "/x/sort/1"),
    ({"path": "a", "sort": [{"path": "a..b"}]}, "value", "/x/sort/0"),
    ({"path": "a", "sort": [{"path": "a", "nulls": "middle"}]}, "value", "/x/sort/0"),
    ({"path": "a", "search": {"text": "x", "whole_word": "yes"}}, "type", "/x/search/whole_word"),
    ({"path": "a", "relpath": "C:\\abs.json" if os.name == "nt" else "/abs.json"}, "value", "/x/relpath"),
    ({"path": "a", "table_layout": [1]}, "type", "/x/table_layout"),
    ({"path": "a", "bookmarks": [{"name": "x", "record": -1}]}, "value", "/x/bookmarks/0"),
    ({"path": "a", "bookmarks": {}}, "type", "/x/bookmarks"),
    ({"path": "a", "mapping": "{}"}, "type", "/x/mapping"),
    ({"path": "a", "edits": "maybe"}, "value", "/x/edits"),
])
def test_strict_reading_names_the_pointer(data, code: str, pointer: str) -> None:
    with pytest.raises(FormatProblem) as info:
        TabState.from_dict(data, pointer="/x", strict=True, workspace=True)
    assert info.value.code == code and info.value.pointer == pointer


def test_json_pointer_escapes() -> None:
    assert ss.json_pointer("tabs", 0, "quick_filters", "a/b~c") == "/tabs/0/quick_filters/a~1b~0c"
    assert ss.json_pointer() == ""


def test_location_texts_of_every_shape() -> None:
    for text in ("#0", "#12.a[0]", "$", "data.items", "[3]", '#5["key with \\"quote\\""]', loc(7, "Тошкент", "ko'cha"),
                 loc(None, "meta", 0, "a b", "1.10", ""), "/data/0", "$.meta"):
        assert TabState.from_dict({"path": "a", "current": text, "expanded": [text]}).current == text
    assert parse_location(loc(7, "Тошкент", "ko'cha")).path == ("Тошкент", "ko'cha")
    assert parse_location(loc(None, "meta", 0, "a b", "1.10", "")).path == ("meta", 0, "a b", "1.10", "")
    # dotted text cannot hold an apostrophe: location texts are always the formatter's
    with pytest.raises(FormatProblem):
        TabState.from_dict({"path": "a", "current": "#7.ko'cha"})
    with pytest.raises(FormatProblem):
        TabState.from_dict({"path": "a", "current": "x" * (ss.LOCATION_MAX_CHARS + 1)})


# ----------------------------------------------------------------------------------------
# budget, setting
# ----------------------------------------------------------------------------------------
def test_fit_session_shortens_the_longest_lists_first() -> None:
    long = [f"#{i}.{'k' * 40}" for i in range(ss.EXPANDED_MAX)]
    session = SessionState(tabs=[TabState(path="a", expanded=list(long)), TabState(path="b", expanded=long[:10])],
                           active=1)
    data = ss.fit_session(session.to_dict(), 30_000)
    assert len(json.dumps(data, ensure_ascii=False).encode()) <= 30_000
    assert [t_["path"] for t_ in data["tabs"]] == ["a", "b"] and data["active"] == 1
    assert len(data["tabs"][0]["expanded"]) < ss.EXPANDED_MAX and data["tabs"][1]["expanded"] == long[:10]


def test_fit_session_is_quick_on_a_huge_session() -> None:
    import time

    tabs = [TabState(path=f"C:\\d\\{i}.json", expanded=[f"#{n}.{'k' * 30}" for n in range(ss.EXPANDED_MAX)])
            for i in range(ss.TABS_MAX)]
    started = time.perf_counter()
    data = ss.session_document(SessionState(tabs=tabs, active=50))
    took = time.perf_counter() - started
    assert len(json.dumps(data, ensure_ascii=False).encode()) <= ss.SESSION_MAX_BYTES
    assert len(data["tabs"]) == ss.TABS_MAX and data["active"] == 50       # lists shortened, no tab lost
    assert all(t_["expanded"] for t_ in data["tabs"]) and took < 3.0


def test_fit_session_drops_tabs_last_but_keeps_the_active_one() -> None:
    tabs = [TabState(path=f"{i}-" + "p" * 2000) for i in range(10)]
    data = ss.fit_session(SessionState(tabs=tabs, active=9).to_dict(), 9_000)
    assert len(json.dumps(data).encode()) <= 9_000 and data["tabs"]
    assert data["active"] is not None and data["tabs"][data["active"]]["path"].startswith("9-")


def test_restore_mode(tmp_path: Path) -> None:
    state = GuiState.load(tmp_path / "s.json")
    assert ss.restore_mode(state) == "always" == ss.RESTORE_DEFAULT
    for mode in ss.RESTORE_MODES:
        state.set(ss.RESTORE_KEY, mode)
        assert ss.restore_mode(state) == mode
    state.set(ss.RESTORE_KEY, "sometimes")
    assert ss.restore_mode(state) == "always"
    assert ss.restore_mode(None) == "always"


# ----------------------------------------------------------------------------------------
# restore plan, paths
# ----------------------------------------------------------------------------------------
def test_plan_skips_missing_files_and_moves_the_active_tab(tmp_path: Path) -> None:
    files = []
    for name in ("a.json", "Тошкент ma'lumot.json", "c.jsonl"):
        p = tmp_path / name
        p.write_text("[]", encoding="utf-8")
        files.append(str(p))
    tabs = [TabState(path=files[0]), TabState(path=str(tmp_path / "gone.json")), TabState(path=files[1]),
            TabState(path=str(tmp_path / "folder")), TabState(path=files[2])]
    (tmp_path / "folder").mkdir()
    plan = ss.plan_restore(tabs, active=2)
    assert [item.path for item in plan.tabs] == files and [item.index for item in plan.tabs] == [0, 2, 4]
    assert [t_.name for t_ in plan.missing] == ["gone.json", "folder"]
    assert plan.active == 1
    # the active tab is missing: the nearest one before it
    assert ss.plan_restore(tabs, active=3).active == 1
    assert ss.plan_restore(tabs, active=1).active == 0
    # nothing before it: the first one
    assert ss.plan_restore(tabs[1:], active=0).active == 0
    assert ss.plan_restore(tabs, active=None).active is None
    assert ss.plan_restore([TabState(path=str(tmp_path / "none.json"))], active=0).tabs == []


def test_relpath_is_tried_first(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    for folder in (old, new):
        (folder / "sub").mkdir(parents=True)
        (folder / "sub" / "d.json").write_text("[]", encoding="utf-8")
    tab = TabState(path=str(old / "sub" / "d.json"), relpath=os.path.join("sub", "d.json"))
    # both exist: the relative path wins (a copied workspace folder uses its own files)
    assert ss.resolve_tab_path(tab, str(new)) == str(new / "sub" / "d.json")
    (new / "sub" / "d.json").unlink()
    assert ss.resolve_tab_path(tab, str(new)) == str(old / "sub" / "d.json")
    # a relative "path" is taken relative to the workspace folder too
    assert ss.resolve_tab_path(TabState(path="sub/d.json"), str(old)) == str(old / "sub" / "d.json")
    assert ss.resolve_tab_path(TabState(path="nothing.json", relpath="no.json"), str(old)) is None
    calls: list[str] = []
    ss.resolve_tab_path(tab, str(new), exists=lambda p: calls.append(p) or False)
    assert calls == [str(new / "sub" / "d.json"), str(old / "sub" / "d.json")]


def test_relative_path(tmp_path: Path) -> None:
    base = tmp_path / "ws"
    assert ss.relative_path(str(base / "a.json"), str(base)) == "a.json"
    assert ss.relative_path(str(base / "x" / "a.json"), str(base)) == os.path.join("x", "a.json")
    assert ss.relative_path(str(tmp_path / "a.json"), str(base)) == os.path.join("..", "a.json")
    if os.name == "nt":
        assert ss.relative_path("Z:\\data\\a.json", "C:\\ws") is None


# ----------------------------------------------------------------------------------------
# capture (duck typing)
# ----------------------------------------------------------------------------------------
class FakeSession:
    def __init__(self, path: str, **kw) -> None:
        self.path = path
        self.format = None
        self.record_path = None
        self.current = None
        self.filter = None
        self.quick_filters: dict = {}
        self.sort_keys: list = []
        self.shown_records = None
        self.__dict__.update(kw)

    @staticmethod
    def location_text(ref) -> str:              # DocSession.location_text
        return loc(ref.record, *ref.path)


class FakePane:
    def __init__(self, kind) -> None:
        self.kind = kind

    def current_kind(self):
        return self.kind


class FakeTree:
    def __init__(self, locations) -> None:
        self.locations = locations
        self.asked: list[int] = []

    def expanded_locations(self, limit):
        self.asked.append(limit)
        return list(self.locations)


class FakeTab:
    def __init__(self, session, kinds=("tree", "text"), trees=(), active=0, second=True, pending_ref=None) -> None:
        self.session = session
        self.panes = [FakePane(k) for k in kinds]
        self.trees = list(trees)
        self.active = active
        self.second = second
        self.pending_ref = pending_ref
        self.closed = False

    def views(self, kind=None):
        return list(self.trees) if kind in (None, "tree") else []

    def active_pane_index(self):
        return self.active

    def second_pane_visible(self):
        return self.second


class FakeSpec:
    def __init__(self, text, lang="expr", builder=None) -> None:
        self.text, self.lang, self.builder = text, lang, builder

    @property
    def empty(self) -> bool:
        return not self.text.strip() and not self.builder

    def expr_text(self) -> str:
        return self.text or "rendered == 1"


class FakeSearchState:
    def __init__(self, inputs=None, opts=None) -> None:
        self.inputs, self.opts = inputs, opts


class FakeOpts:
    def to_dict(self):
        return {"text": "Ўзбек", "mode": "regex", "case_sensitive": True, "whole_word": False, "translit": False,
                "scope": "keys", "path": None}


class FakePanel:
    def __init__(self, current=None, inputs=None, states=None) -> None:
        self._current, self._inputs, self._states = current, inputs or {}, states or {}

    def session(self):                          # the real panel: a method
        return self._current

    def input_values(self):
        return dict(self._inputs)

    def state(self, session):
        return self._states.get(id(session))


def test_capture_tab_takes_what_the_window_shows(tmp_path: Path) -> None:
    s = FakeSession(str(tmp_path / "a.json"), format="wrapped", record_path=("data", 0),
                    current=Ref(12, ("contact", KEY_TRICKY)), filter=FakeSpec("age > 30 and city == 'Тошкент'"),
                    quick_filters={"name": "~^A", "zip": "   "}, sort_keys=[SortKey("name"), SortKey("id", True)])
    tree = FakeTree(["#0", "#12", "x" * (ss.LOCATION_MAX_CHARS + 1), "#12.contact", 7, ""])
    tab = FakeTab(s, kinds=("table", "tree"), trees=[tree], active=1, second=False)
    ts = ss.capture_tab(tab)
    assert ts.path == str(tmp_path / "a.json") and ts.format == "wrapped" and ts.record_path == ["data", 0]
    assert ts.views == ["table", "tree"] and ts.active_pane == 1 and ts.second_pane is False
    assert ts.current == FakeSession.location_text(Ref(12, ("contact", KEY_TRICKY)))
    assert parse_location(ts.current).path == ("contact", KEY_TRICKY)
    assert ts.expanded == ["#0", "#12", "#12.contact"] and tree.asked == [ss.EXPANDED_MAX]
    assert ts.filter == {"text": "age > 30 and city == 'Тошкент'", "lang": "expr", "builder": None}
    assert ts.quick_filters == {"name": "~^A"}
    assert [SortKey(**{**d}) for d in ts.sort] == [SortKey("name"), SortKey("id", True)]
    # the result reads back strictly
    assert TabState.from_dict(through_json(ts.to_dict()), strict=True) == ts


def test_capture_of_search_results_keeps_no_filter_or_sort(tmp_path: Path) -> None:
    s = FakeSession(str(tmp_path / "a.json"), shown_records=object(), filter=FakeSpec("x > 1"),
                    sort_keys=[SortKey("x")], quick_filters={"x": "== 1"})
    ts = ss.capture_tab(FakeTab(s))
    assert ts.filter is None and ts.sort == [] and ts.quick_filters == {}


def test_capture_takes_the_pending_reveal_while_opening(tmp_path: Path) -> None:
    s = FakeSession(str(tmp_path / "a.json"))
    assert ss.capture_tab(FakeTab(s, pending_ref=Ref(None, ("meta", "v")))).current == "meta.v"
    assert ss.capture_tab(FakeTab(s)).current is None
    assert ss.capture_tab(FakeTab(FakeSession(""))) is None


def test_filter_dict() -> None:
    assert ss.filter_dict(None) is None and ss.filter_dict(FakeSpec("  ")) is None
    assert ss.filter_dict(FakeSpec("a", "jmespath")) == {"text": "a", "lang": "jmespath", "builder": None}
    builder = {"op": "and", "negate": False, "items": [{"field": "a", "op": "eq", "value": 1, "value2": None}]}
    assert ss.filter_dict(FakeSpec("a == 1", builder=builder))["builder"] == builder
    # a builder that is not plain JSON: dropped, the text (rendered from it when empty) stays
    odd = {"op": "and", "items": [{"field": "a", "op": "eq", "value": Decimal("1.10")}]}
    assert ss.filter_dict(FakeSpec("a == 1.10", builder=odd)) == {"text": "a == 1.10", "lang": "expr",
                                                                    "builder": None}
    assert ss.filter_dict(FakeSpec("", builder=odd))["text"] == "rendered == 1"
    assert ss.filter_dict(FakeSpec("x", "sql")) is None


def test_search_inputs_shown_remembered_or_from_the_last_search() -> None:
    s1, s2, s3 = FakeSession("a"), FakeSession("b"), FakeSession("c")
    shown = {"text": "O'zbekiston", "mode": "plain", "case_sensitive": False, "whole_word": True,
             "translit": True, "scope": "both", "path": ""}
    panel = FakePanel(current=s1, inputs=shown,
                      states={id(s2): FakeSearchState(inputs={"text": "Тошкент", "mode": "plain"}),
                              id(s3): FakeSearchState(opts=FakeOpts())})
    assert ss.search_inputs(panel, s1) == shown
    assert ss.search_inputs(panel, s2) == {"text": "Тошкент", "mode": "plain"}
    assert ss.search_inputs(panel, s3) == {"text": "Ўзбек", "mode": "regex", "case_sensitive": True,
                                           "whole_word": False, "translit": False, "scope": "keys"}
    assert ss.search_inputs(panel, FakeSession("d")) is None
    assert ss.search_inputs(FakePanel(current=s1, inputs={"text": "  "}), s1) is None
    assert ss.search_inputs(FakePanel(current=s1, inputs={"text": "x", "translit": "on"}), s1) is None
    assert ss.search_inputs(None, s1) is None


class FakeWindow:
    def __init__(self, tabs, current=None, panels=None) -> None:
        self.tabs, self.current, self.panels = tabs, current, panels or {}

    def tab_widgets(self):
        return list(self.tabs)

    def current_tab(self):
        return self.current


def test_capture_window_order_active_extra_and_limit(tmp_path: Path) -> None:
    tabs = [FakeTab(FakeSession(str(tmp_path / f"{i}.json"))) for i in range(3)]
    tabs[1].closed = True
    seen: list[str] = []

    def extra(tab, ts):
        seen.append(ts.name)
        ts.current = "#1"

    session = ss.capture_window(FakeWindow(tabs, current=tabs[2]), extra=extra)
    assert [t_.name for t_ in session.tabs] == ["0.json", "2.json"] and session.active == 1
    assert seen == ["0.json", "2.json"] and all(t_.current == "#1" for t_ in session.tabs)
    many = [FakeTab(FakeSession(f"{i}.json")) for i in range(ss.TABS_MAX + 4)]
    assert len(ss.capture_window(FakeWindow(many)).tabs) == ss.TABS_MAX
    # the search panel of the window gives the inputs
    s = FakeSession("q.json")
    panel = FakePanel(current=s, inputs={"text": "Toshkent"})
    assert ss.capture_window(FakeWindow([FakeTab(s)], panels={"search": panel})).tabs[0].search == {"text": "Toshkent"}


def test_sort_key_dicts() -> None:
    key = SortKey("contact.address.city", descending=True, nulls="first", natural=False, collation="unicode",
                  case_sensitive=True)
    assert ss.sort_key_from_dict(ss.sort_key_dict(key)) == key
    assert ss.sort_key_from_dict({"path": "$key"}) == SortKey("$key")
    with pytest.raises(TypeError):
        ss.sort_key_from_dict(["name"])
