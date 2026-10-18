"""GuiState (jsonreshape/gui/guistate.py): the Qt-free GUI state file.

Settings get/set with defaults and validation, save/load round trips, tolerant loading
(corrupt file kept as .bad), recent files (order, dedupe by normalized path, limit, merge
of two writers), bookmarks per normalized path, table layouts (LRU limit), merging of
concurrent writers (last writer wins per key), failures and the inter-process lock.
"""

from __future__ import annotations

import itertools
import json
import os
import sys
import threading
from pathlib import Path

import pytest

from jsonreshape.errors import JsonReshapeError
from jsonreshape.gui import guistate
from jsonreshape.gui.guistate import DEFAULTS, LAYOUTS_MAX, RECENT_MAX, Bookmark, GuiState, norm_path


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "gui_state.json"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    """A strictly increasing time.time() for the module (deterministic LRU order)."""
    counter = itertools.count(1_000_000)
    monkeypatch.setattr(guistate.time, "time", lambda: float(next(counter)))


def test_default_location_is_app_home(_isolated_app_home: Path) -> None:
    st = GuiState.load()
    assert st.path == Path(os.environ["JSONRESHAPE_HOME"]) / "gui_state.json"
    assert st.save()
    assert st.path.exists()


def test_settings_defaults_and_validation(state_path: Path) -> None:
    st = GuiState.load(state_path)
    assert st.get("theme") == "system"
    assert st.get("mono_size") == DEFAULTS["mono_size"]
    assert st.get("unknown") is None
    assert st.get("unknown", 5) == 5
    st.set("theme", "dark")
    assert st.get("theme") == "dark"
    with pytest.raises(ValueError):
        st.set("theme", "blue")
    with pytest.raises(TypeError):
        st.set("geometry", object())
    with pytest.raises(TypeError):
        st.set("x", float("nan"))
    with pytest.raises(TypeError):
        st.set("", 1)
    value = {"a": [1, 2]}
    st.set("custom", value)
    value["a"].append(3)                 # the state keeps its own copy
    assert st.get("custom") == {"a": [1, 2]}
    st.set("custom", None)               # None removes
    assert st.get("custom") is None
    assert "custom" not in st.keys()


def test_save_load_round_trip(state_path: Path) -> None:
    st = GuiState.load(state_path)
    assert not st.dirty
    st.set("theme", "light")
    st.set("mono_font", "Consolas")
    st.set("mono_size", 11)
    st.set("window_state", "AAAA/w==")
    st.add_recent(state_path.parent / "a.json")
    assert st.dirty
    assert st.save()
    assert not st.dirty
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    again = GuiState.load(state_path)
    assert again.get("theme") == "light"
    assert again.get("mono_font") == "Consolas"
    assert again.get("mono_size") == 11
    assert again.get("window_state") == "AAAA/w=="
    assert again.recent_files() == [os.path.abspath(state_path.parent / "a.json")]


def test_corrupt_file_is_kept_as_bad(state_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    state_path.parent.mkdir(parents=True)
    state_path.write_bytes(b'{"settings": {"theme": "dark"')     # truncated
    with caplog.at_level("WARNING"):
        st = GuiState.load(state_path)
    assert st.get("theme") == "system"
    bad = state_path.with_name("gui_state.json.bad")
    assert bad.read_bytes() == b'{"settings": {"theme": "dark"'
    assert not state_path.exists()
    assert any("corrupt" in r.getMessage() for r in caplog.records)
    st.set("theme", "dark")
    assert st.save()
    assert GuiState.load(state_path).get("theme") == "dark"


def test_wrong_layout_parts_are_ignored(state_path: Path, tmp_path: Path) -> None:
    state_path.parent.mkdir(parents=True)
    x, y = norm_path(tmp_path / "x.json"), norm_path(tmp_path / "y.json")
    state_path.write_text(json.dumps({
        "version": 99,
        "settings": {"theme": "dark", "1": 2},
        "recent": ["a.json", 5, "", "A.JSON" if sys.platform == "win32" else "a.json", "b.json"],
        "bookmarks": {x: {"items": [{"name": "ok", "record": 1, "path": ["a", 0]},
                                    {"name": "bad", "record": "x", "path": []},
                                    {"name": "bad2", "record": 1, "path": [1.5]},
                                    "nonsense"]},
                      y: "nonsense"},
        "layouts": {x: {"layout": {"cols": ["a"]}, "used": 3}, y: {"layout": 5}},
    }), encoding="utf-8")
    st = GuiState.load(state_path)
    assert st.get("theme") == "dark"
    assert st.recent_files() == ["a.json", "b.json"]
    assert [(b.name, b.record, b.path) for b in st.bookmarks(tmp_path / "x.json")] == [("ok", 1, ["a", 0])]
    assert st.bookmarks(tmp_path / "y.json") == []
    assert st.table_layout(tmp_path / "x.json") == {"cols": ["a"]}
    assert st.table_layout(tmp_path / "y.json") is None
    assert not state_path.with_name("gui_state.json.bad").exists()


def test_recent_files_order_dedupe_limit(state_path: Path, tmp_path: Path) -> None:
    st = GuiState.load(state_path)
    files = [tmp_path / f"f{i}.json" for i in range(RECENT_MAX + 5)]
    for f in files:
        st.add_recent(f)
    recent = st.recent_files()
    assert len(recent) == RECENT_MAX
    assert recent[0] == os.path.abspath(files[-1])
    assert recent[-1] == os.path.abspath(files[5])
    st.add_recent(files[10])                          # moves to the front, no duplicate
    recent = st.recent_files()
    assert recent[0] == os.path.abspath(files[10])
    assert len(recent) == RECENT_MAX
    assert len({norm_path(p) for p in recent}) == RECENT_MAX
    if sys.platform == "win32":
        st.add_recent(str(files[12]).upper())       # same file, other case
        recent = st.recent_files()
        assert recent[0] == str(os.path.abspath(str(files[12]).upper()))
        assert sum(norm_path(p) == norm_path(files[12]) for p in recent) == 1
    st.remove_recent(files[10])
    assert norm_path(files[10]) not in {norm_path(p) for p in st.recent_files()}
    st.clear_recent()
    assert st.recent_files() == []
    assert st.save()
    assert GuiState.load(state_path).recent_files() == []


def test_two_writers_merge_recent_files(state_path: Path, tmp_path: Path) -> None:
    a = GuiState.load(state_path)
    b = GuiState.load(state_path)
    a.add_recent(tmp_path / "x.json")
    b.add_recent(tmp_path / "y.json")
    assert a.save()
    assert b.save()
    merged = GuiState.load(state_path).recent_files()
    assert merged == [os.path.abspath(tmp_path / "y.json"), os.path.abspath(tmp_path / "x.json")]
    assert b.recent_files() == merged                 # b sees a's change after saving
    a.remove_recent(tmp_path / "y.json")              # a never saw y: the removal still applies
    assert a.save()
    assert GuiState.load(state_path).recent_files() == [os.path.abspath(tmp_path / "x.json")]


def test_two_writers_last_writer_wins_per_key(state_path: Path) -> None:
    a = GuiState.load(state_path)
    b = GuiState.load(state_path)
    a.set("theme", "dark")
    a.set("mono_font", "Consolas")
    b.set("theme", "light")
    b.set("ui_size", 12)
    assert a.save()
    assert b.save()
    final = GuiState.load(state_path)
    assert final.get("theme") == "light"              # b wrote it last
    assert final.get("mono_font") == "Consolas"       # a's other key survives
    assert final.get("ui_size") == 12
    a.reload()
    assert a.get("ui_size") == 12 and a.get("theme") == "light"
    b.set("mono_font", None)                          # a removal is merged too
    assert b.save()
    assert GuiState.load(state_path).get("mono_font") == ""   # back to the default


def test_bookmarks_per_normalized_path(state_path: Path, tmp_path: Path) -> None:
    st = GuiState.load(state_path)
    src = tmp_path / "Data.json"
    marks = [Bookmark("first", 3, ["a", 0, "b"], created=1.5), Bookmark("root", None, [])]
    st.set_bookmarks(src, marks)
    other = str(src).lower() if sys.platform == "win32" else str(src)
    got = st.bookmarks(other)
    assert [(b.name, b.record, b.path, ) for b in got] == [("first", 3, ["a", 0, "b"]), ("root", None, [])]
    assert got[0].created == 1.5
    assert st.bookmarks(tmp_path / "nothing.json") == []
    assert st.save()
    again = GuiState.load(state_path)
    assert [b.to_dict() for b in again.bookmarks(src)] == [b.to_dict() for b in marks]
    again.set_bookmarks(src, [])
    assert again.save()
    assert GuiState.load(state_path).bookmarks(src) == []
    with pytest.raises(TypeError):
        st.set_bookmarks(src, [{"name": "x"}])


def test_bookmark_validation_and_dict_round_trip() -> None:
    bm = Bookmark("n", 0, ("a", 1))
    assert bm.path == ["a", 1]
    assert Bookmark.from_dict(bm.to_dict()) == bm
    with pytest.raises(ValueError):
        Bookmark("n", -1, [])
    with pytest.raises(ValueError):
        Bookmark("n", True, [])
    with pytest.raises(ValueError):
        Bookmark("n", 1, [True])
    with pytest.raises(TypeError):
        Bookmark.from_dict({"name": 5, "record": 1, "path": []})
    with pytest.raises(TypeError):
        Bookmark.from_dict({"name": "x", "record": 1, "path": "a.b"})


def test_two_writers_bookmarks_of_different_files(state_path: Path, tmp_path: Path) -> None:
    a = GuiState.load(state_path)
    b = GuiState.load(state_path)
    a.set_bookmarks(tmp_path / "one.json", [Bookmark("a1", 1, [])])
    b.set_bookmarks(tmp_path / "two.json", [Bookmark("b1", 2, [])])
    assert a.save() and b.save()
    final = GuiState.load(state_path)
    assert [x.name for x in final.bookmarks(tmp_path / "one.json")] == ["a1"]
    assert [x.name for x in final.bookmarks(tmp_path / "two.json")] == ["b1"]


def test_table_layouts_lru_limit(state_path: Path, tmp_path: Path, clock) -> None:
    st = GuiState.load(state_path)
    for i in range(LAYOUTS_MAX + 5):
        st.set_table_layout(tmp_path / f"t{i}.json", {"columns": [f"c{i}"], "widths": [100]})
    assert st.table_layout(tmp_path / "t0.json") is None              # the oldest were dropped
    assert st.table_layout(tmp_path / "t4.json") is None
    assert st.table_layout(tmp_path / "t5.json") == {"columns": ["c5"], "widths": [100]}
    assert st.save()
    again = GuiState.load(state_path)
    assert len(again._data["layouts"]) == LAYOUTS_MAX
    # reading a layout marks it used: it survives the next additions
    assert again.table_layout(tmp_path / "t5.json") is not None
    again.set_table_layout(tmp_path / "new.json", {"columns": []})
    assert again.table_layout(tmp_path / "t5.json") is not None
    assert again.table_layout(tmp_path / "t6.json") is None
    layout = again.table_layout(tmp_path / "new.json")
    layout["columns"].append("mutated")                                 # a copy
    assert again.table_layout(tmp_path / "new.json") == {"columns": []}
    again.set_table_layout(tmp_path / "new.json", None)
    assert again.table_layout(tmp_path / "new.json") is None
    with pytest.raises(TypeError):
        again.set_table_layout(tmp_path / "x.json", ["not", "a", "dict"])


def test_save_failure_is_reported(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    st = GuiState.load(blocker / "gui_state.json")       # the parent is a file
    st.set("theme", "dark")
    assert st.save() is False
    assert st.dirty                                      # nothing was lost
    with pytest.raises((JsonReshapeError, OSError)):
        st.save(raise_errors=True)


def test_lock_held_by_another_writer(state_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jsonreshape.query import file_lock

    monkeypatch.setattr(guistate, "LOCK_TIMEOUT", 0.2)
    st = GuiState.load(state_path)
    st.set("theme", "dark")
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with file_lock(state_path, "gui.state.busy"):
            held.set()
            release.wait(10)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert held.wait(10)
    try:
        with pytest.raises(JsonReshapeError) as info:
            st.save(raise_errors=True)
        assert info.value.key == "gui.state.busy"
        assert st.save() is False
    finally:
        release.set()
        thread.join(10)
    assert st.save() is True
    assert GuiState.load(state_path).get("theme") == "dark"
