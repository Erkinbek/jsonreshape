"""Workspace files (jsonreshape/gui/workspace.py, the Qt-free part; docs/ARCHITECTURE.md §15.0 "Workspace
file"): the ``*.jrworkspace`` round trip (every tab field, Uzbek Latin / Cyrillic / apostrophes, exact numbers,
an embedded mapping file -- also one deeper than the JSON decoders' recursion limits), ``relpath`` relative to
the workspace file (a moved workspace folder still finds its files; another drive has none), the atomic write
(an interrupted save leaves the old file and no temp file), strict loading (every bad part refused with its
JSON pointer, localized), the size limit, a missing file, and the recent-workspaces list in the GUI state."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jsonreshape import codec, fileio
from jsonreshape import mapping as mp
from jsonreshape.codec import JsonFloat
from jsonreshape.errors import JsonReshapeError, UserInputError
from jsonreshape.gui import sessionstore as ss
from jsonreshape.gui import workspace as wsm
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.sessionstore import TabState
from jsonreshape.gui.workspace import Workspace, WorkspaceError
from jsonreshape.i18n import set_language, t

RECORDS = [{"id": 0, "name": "Oʻzbekiston", "price": JsonFloat("1.10"), "big": 12345678901234567890,
            "contact": {"city": "Тошкент", "street": "Amir Temur ko'chasi", "tags": ["a", "b"]}},
           {"id": 1, "name": "G'ulom", "price": JsonFloat("1e400"), "big": -1,
            "contact": {"city": "Samarqand", "street": None, "tags": []}}]


def mapping_tree() -> tuple[mp.MappingTree, mp.MappingTree]:
    """A mapping tree with a user change (rename) and its original."""
    tree = mp.infer_tree(RECORDS, root_kind="records")
    designer = mp.Designer(tree, population=lambda: iter(RECORDS), undo_limit=200)
    assert designer.rename(designer.tree.find("[].contact"), "manzil Ўзбек").applied
    return designer.tree, designer.orig


def make_tab(path: Path, **kw) -> TabState:
    tree, orig = mapping_tree()
    data = dict(
        path=str(path), format=None, record_path="data", views=["tree", "mapping"], active_pane=1,
        current="#1.contact.city", expanded=["#0", "#1", "#1.contact"],
        filter={"text": "price == 1.10 and big == 12345678901234567890", "lang": "expr",
                "builder": {"op": "and", "negate": False,
                            "items": [{"field": "price", "op": "eq", "value": JsonFloat("1.10"), "value2": None},
                                      {"field": "big", "op": "eq", "value": 12345678901234567890,
                                       "value2": None}]}},
        quick_filters={"name": "~^O'z"}, sort=[ss.sort_key_dict(mp_sort("contact.city"))],
        search={"text": "Тошкент", "mode": "plain", "translit": True},
        table_layout={"version": 2, "kind": "array", "record_path": None,
                      "columns": [{"path": "name", "width": 140, "hidden": False}], "frozen": 1, "filter_row": True},
        bookmarks=[{"name": "Bosh sahifa — ko'cha", "record": 1, "path": ["contact", "city"], "created": 1760000000.5}],
        mapping=wsm.mapping_document(tree, input_info={"source": str(path), "kind": "array", "sample": 2}, orig=orig),
        edits="pending")
    data.update(kw)
    return TabState(**data)


def mp_sort(path: str):
    from jsonreshape.sortkeys import SortKey

    return SortKey(path, descending=True, collation="uz")


# ----------------------------------------------------------------------------------------
# round trip
# ----------------------------------------------------------------------------------------
def test_write_and_read_back_every_field(tmp_path: Path) -> None:
    data_file = tmp_path / "Тошкент maʼlumot.json"
    other = tmp_path / "sub" / "b.jsonl"
    ws = Workspace(tabs=[make_tab(data_file), TabState(path=str(other))], active=1,
                   geometry={"window": "AAEC", "state": "AQID"})
    target = tmp_path / "loyiha.jrworkspace"
    assert wsm.write_workspace(ws, target) == str(target)
    raw = target.read_bytes()
    assert raw.endswith(b"\n") and "Тошкент maʼlumot.json".encode() in raw            # UTF-8, not escaped
    assert b'\n      "expanded": ["#0","#1","#1.contact"],\n' in raw                    # one member per line
    doc = json.loads(raw.decode("utf-8"))
    assert doc["version"] == 1 and doc["active"] == 1 and isinstance(doc["saved"], str) and doc["app"]
    assert list(doc["tabs"][0])[:3] == ["path", "relpath", "format"]
    assert doc["tabs"][0]["relpath"] == data_file.name
    assert doc["tabs"][1]["relpath"] == os.path.join("sub", "b.jsonl")
    back = wsm.read_workspace(target)
    assert back.path == str(target) and back.active == 1 and back.geometry == {"window": "AAEC", "state": "AQID"}
    first = make_tab(data_file, relpath=data_file.name)
    assert back.tabs[0] == first
    assert back.tabs[1] == TabState(path=str(other), relpath=os.path.join("sub", "b.jsonl"))
    # exact numbers: the builder's values keep their text
    values = [item["value"] for item in back.tabs[0].filter["builder"]["items"]]
    assert codec.dumps(values) == b"[1.10,12345678901234567890]"
    # the embedded mapping was validated and parsed once
    tree, info = back.mappings[0]
    expect, orig = mapping_tree()
    assert mp.dumps_mapping(tree) == mp.dumps_mapping(expect, orig=orig)
    assert tree.find("[].manzil Ўзбек") is not None and tree.orig is not None
    assert info["source"] == str(data_file) and info["sample"] == 2
    assert 1 not in back.mappings


def test_moved_workspace_folder_finds_its_files(tmp_path: Path) -> None:
    folder = tmp_path / "loyiha"
    (folder / "data").mkdir(parents=True)
    a = folder / "data" / "a.json"
    a.write_text("[1]", encoding="utf-8")
    elsewhere = tmp_path / "other.json"
    elsewhere.write_text("[2]", encoding="utf-8")
    ws = Workspace(tabs=[TabState(path=str(a)), TabState(path=str(elsewhere)),
                         TabState(path=str(folder / "data" / "gone.json"))], active=0)
    wsm.write_workspace(ws, folder / "w.jrworkspace")
    moved = tmp_path / "ko'chirilgan papka"
    os.rename(folder, moved)
    back = wsm.read_workspace(moved / "w.jrworkspace")
    assert back.tabs[0].path == str(a) and not a.exists()          # the absolute path is the old one
    plan = ss.plan_restore(back.tabs, active=back.active, base_dir=str(moved))
    assert [item.path for item in plan.tabs] == [str(moved / "data" / "a.json"), str(elsewhere)]
    assert [tab.name for tab in plan.missing] == ["gone.json"] and plan.active == 0


@pytest.mark.skipif(os.name != "nt", reason="drive letters")
def test_a_file_on_another_drive_has_no_relpath(tmp_path: Path) -> None:
    drive = os.path.splitdrive(str(tmp_path))[0].upper()
    other = "Q:" if drive != "Q:" else "R:"
    ws = Workspace(tabs=[TabState(path=other + "\\data\\x.json")])
    doc = json.loads(wsm.dumps_workspace(ws, str(tmp_path / "w.jrworkspace")))
    assert doc["tabs"][0]["relpath"] is None and doc["tabs"][0]["path"] == other + "\\data\\x.json"


def test_deep_embedded_mapping(tmp_path: Path) -> None:
    """A mapping of a very deep document is deeper than orjson / json allow: written and read anyway."""
    value: dict = {"leaf": 1}
    for i in range(700):
        value = {f"k{i % 3}": value}
    tree = mp.infer_tree([value], root_kind="records", max_depth=None, max_keys=None, max_nodes=None)
    tab = TabState(path=str(tmp_path / "deep.json"), mapping=wsm.mapping_document(tree))
    target = tmp_path / "deep.jrworkspace"
    wsm.write_workspace(Workspace(tabs=[tab]), target)
    back = wsm.read_workspace(target)
    assert mp.dumps_mapping(back.mappings[0][0]) == mp.dumps_mapping(tree)


def test_bom_unknown_keys_and_minimal_tabs(tmp_path: Path) -> None:
    target = tmp_path / "w.jrworkspace"
    doc = {"version": 1, "future_key": {"x": 1}, "tabs": [{"path": "C:\\a.json", "new_field": 1}]}
    target.write_bytes(b"\xef\xbb\xbf" + json.dumps(doc).encode())
    back = wsm.read_workspace(target)
    assert back.tabs == [TabState(path="C:\\a.json")] and back.active is None and back.geometry is None


# ----------------------------------------------------------------------------------------
# crash safety
# ----------------------------------------------------------------------------------------
def test_an_interrupted_save_leaves_the_old_file(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "w.jrworkspace"
    wsm.write_workspace(Workspace(tabs=[TabState(path=str(tmp_path / "old.json"))]), target)
    before = target.read_bytes()

    def crash(*_args, **_kw):
        raise OSError(5, "simulated crash before the replace")

    monkeypatch.setattr(fileio, "_replace", crash)
    with pytest.raises(OSError):
        wsm.write_workspace(Workspace(tabs=[TabState(path=str(tmp_path / "new.json"))]), target)
    assert target.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["w.jrworkspace"]      # no temp file left


def test_a_failing_write_leaves_the_old_file(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "w.jrworkspace"
    wsm.write_workspace(Workspace(tabs=[TabState(path=str(tmp_path / "old.json"))]), target)
    before = target.read_bytes()

    def full(*_args, **_kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", full)
    with pytest.raises(JsonReshapeError):
        wsm.write_workspace(Workspace(tabs=[TabState(path=str(tmp_path / "new.json"))]), target)
    assert target.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["w.jrworkspace"]


# ----------------------------------------------------------------------------------------
# strict loading
# ----------------------------------------------------------------------------------------
def _doc(**kw) -> bytes:
    doc = {"version": 1, "tabs": [{"path": "C:\\a.json"}]}
    doc.update(kw)
    return json.dumps(doc).encode()


def _bad_mapping() -> dict:
    tree = mp.infer_tree(RECORDS, root_kind="records")
    obj = wsm.mapping_document(tree)
    obj["tree"]["children"][0]["children"][1]["kind"] = "table"
    return obj


@pytest.mark.parametrize("data, key, pointer", [
    (b"{\"version\": 1, ", "workspace.error.json", ""),
    (b"\xff\xfe{}", "workspace.error.json", ""),
    (b"[]", "workspace.error.not_object", ""),
    (json.dumps({"tabs": []}).encode(), "workspace.error.missing", "/version"),
    (json.dumps({"version": 2, "tabs": []}).encode(), "workspace.error.version", ""),
    (json.dumps({"version": "1", "tabs": []}).encode(), "workspace.error.version", ""),
    (json.dumps({"version": 1}).encode(), "workspace.error.missing", "/tabs"),
    (_doc(tabs={}), "workspace.error.type", "/tabs"),
    (_doc(tabs=[{"path": "a"}] * (ss.TABS_MAX + 1)), "workspace.error.too_many", "/tabs"),
    (_doc(tabs=[{"path": "a"}, {"name": "b"}]), "workspace.error.missing", "/tabs/1/path"),
    (_doc(tabs=[{"path": "a", "expanded": ["#0", "#1.", "#2"]}]), "workspace.error.value", "/tabs/0/expanded/1"),
    (_doc(tabs=[{"path": "a", "views": ["tree", 3]}]), "workspace.error.type", "/tabs/0/views/1"),
    (_doc(tabs=[{"path": "a", "bookmarks": [{"name": 1, "record": 0}]}]), "workspace.error.value",
     "/tabs/0/bookmarks/0"),
    (_doc(tabs=[{"path": "a", "quick_filters": {"a/b": 5}}]), "workspace.error.type", "/tabs/0/quick_filters/a~1b"),
    (_doc(tabs=[{"path": "a", "mapping": {"version": 1}}]), "workspace.error.mapping", "/tabs/0/mapping"),
    (_doc(tabs=[{"path": "a", "mapping": _bad_mapping()}]), "workspace.error.mapping",
     "/tabs/0/mapping/tree/children/0/children/1"),
    (_doc(active=1), "workspace.error.value", "/active"),
    (_doc(active=True), "workspace.error.value", "/active"),
    (_doc(geometry="AAEC"), "workspace.error.type", "/geometry"),
    (_doc(geometry={"window": 5}), "workspace.error.type", "/geometry/window"),
    (_doc(geometry={"window": "not base64!"}), "workspace.error.value", "/geometry/window"),
    (_doc(app=1), "workspace.error.type", "/app"),
])
def test_strict_loading_names_the_bad_part(data: bytes, key: str, pointer: str) -> None:
    with pytest.raises(WorkspaceError) as info:
        wsm.loads_workspace(data)
    assert info.value.key == key and info.value.pointer == pointer
    assert pointer in info.value.message and info.value.message != key


def test_error_messages_are_localized(lang_uz) -> None:
    with pytest.raises(WorkspaceError) as info:
        wsm.loads_workspace(_doc(tabs=[{"path": "a", "views": "tree"}]))
    uz = info.value.message
    assert uz == "Workspace faylidagi /tabs/0/views noto'g'ri: ro'yxat (JSON massiv) bo'lishi kerak."
    set_language("en")
    assert info.value.message == "/tabs/0/views in the workspace file is wrong: it must be a list (JSON array)."
    set_language("uz")
    for name in ("string", "bool", "int", "list", "object", "string_or_list"):
        assert t(f"workspace.type.{name}") != f"workspace.type.{name}"
    for code in ("missing", "type", "value", "too_many"):
        assert t(f"workspace.error.{code}") != f"workspace.error.{code}"


def test_size_limit_and_missing_file(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "w.jrworkspace"
    target.write_bytes(_doc())
    monkeypatch.setattr(wsm, "MAX_FILE_BYTES", 10)
    with pytest.raises(WorkspaceError) as info:
        wsm.read_workspace(target)
    assert info.value.key == "workspace.error.too_big"
    with pytest.raises(UserInputError) as missing:
        wsm.read_workspace(tmp_path / "yo'q.jrworkspace")
    assert missing.value.key == "error.file_not_found"


# ----------------------------------------------------------------------------------------
# recent workspaces
# ----------------------------------------------------------------------------------------
def test_recent_workspaces(tmp_path: Path) -> None:
    state = GuiState.load(tmp_path / "gui_state.json")
    assert wsm.recent_workspaces(state) == []
    for i in range(wsm.RECENT_MAX + 3):
        wsm.add_recent_workspace(state, str(tmp_path / f"w{i}.jrworkspace"))
    names = [os.path.basename(p) for p in wsm.recent_workspaces(state)]
    assert len(names) == wsm.RECENT_MAX and names[0] == f"w{wsm.RECENT_MAX + 2}.jrworkspace"
    again = str(tmp_path / "w5.jrworkspace")
    if os.name == "nt":
        again = again.upper()                     # one file, another spelling: moved to the front once
    wsm.add_recent_workspace(state, again)
    items = wsm.recent_workspaces(state)
    assert items[0] == os.path.abspath(again) and len(items) == wsm.RECENT_MAX
    assert sum(1 for p in items if os.path.normcase(p) == os.path.normcase(again)) == 1
    wsm.remove_recent_workspace(state, items[1])
    assert items[1] not in wsm.recent_workspaces(state)
    state.save(raise_errors=True)
    assert wsm.recent_workspaces(GuiState.load(tmp_path / "gui_state.json"))[0] == os.path.abspath(again)
    wsm.clear_recent_workspaces(state)
    assert wsm.recent_workspaces(state) == []
    state.set(wsm.RECENT_KEY, ["a", 5, None, ""])
    assert wsm.recent_workspaces(state) == ["a"]


def test_report_text_is_one_line(lang_uz) -> None:
    report = wsm.RestoreReport("session", opened=[1, 2], missing=["a.json", "b.json", "c.json", "Тошкент.json"])
    text = wsm.report_text(report)
    assert text == ("Oxirgi sessiya tiklandi: 2 ta fayl. 4 ta fayl topilmadi va ochilmadi: a.json, b.json, c.json "
                    "va yana 1 ta.")
    ws = wsm.RestoreReport("workspace", opened=[1], pending_edits=1,
                           path=os.path.join(os.path.abspath("w"), "loyiha.jrworkspace"))   # (this system's path)
    assert wsm.report_text(ws).startswith("Workspace ochildi: loyiha.jrworkspace (1 ta fayl). 1 ta faylda")
    assert "\n" not in wsm.report_text(ws)
