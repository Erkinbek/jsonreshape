"""The raw and schema views and the node reading helpers on edited documents (ARCHITECTURE §12.9).

* Raw: the current node is highlighted / jumped to only while its span is in the data file; an edited (or new) node
  gets the "edited -- the raw view shows the original file" note; undo brings the highlight back.
* Schema: stale after edits (header note), computed again when visible (the new field appears), when activated if
  hidden; a full scan after edits describes the effective data.
* Node readers (``widgets``): ``task_doc`` / ``task_node`` (one pinned state), ``node_text_chunks`` / ``load_full_text``
  / ``copy_node_value`` / ``run_value_task`` give the EFFECTIVE value of edited nodes and of composite nodes (the root
  of an edited array, the wrapper of an edited wrapped document, the JSON Lines root), ``resolve_in``,
  ``edited_span``, ``remap_node_ref``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from jsonreshape import codec  # noqa: E402
from jsonreshape.gui import fields, widgets  # noqa: E402
from jsonreshape.gui.rawview import RawView  # noqa: E402
from jsonreshape.gui.schemaview import SchemaView  # noqa: E402
from jsonreshape.gui.session import NodeRef  # noqa: E402
from jsonreshape.overlay import EDIT_BASE  # noqa: E402
from jsonreshape.reader import PinnedDocument  # noqa: E402

from views_edit_helpers import (  # noqa: E402,F401
    TIMEOUT,
    app,
    applied,
    keep_selection,
    no_logged_errors,
    opened,
    records,
    views,
    write,
    write_json,
)

pytestmark = [pytest.mark.gui, pytest.mark.usefixtures("no_logged_errors")]


def settle(v: RawView, timeout: float = TIMEOUT / 1000) -> None:
    import time

    deadline = time.monotonic() + timeout
    while not v.is_settled():
        QApplication.processEvents()
        if time.monotonic() > deadline:
            raise AssertionError("raw view did not settle")
        threading.Event().wait(0.002)


def clipboard() -> str:
    return QGuiApplication.clipboard().text()


# ----------------------------------------------------------------------------------
# raw view
# ----------------------------------------------------------------------------------
def test_raw_view_highlights_file_nodes_only(qtbot, opened, views, tmp_path) -> None:
    data = [{"id": i, "s": f"value {i}"} for i in range(50)]
    path = write_json(tmp_path / "a.json", data)
    s = opened(path)
    v = views(RawView, s)
    settle(v)
    node = s.doc.record_node(30)
    s.select(NodeRef(30, ()))
    qtbot.waitUntil(lambda: v.area.node_span() == (node.start, node.end), timeout=TIMEOUT)
    assert not v.edited_note_shown()
    # the current node's record is edited: no highlight, the note instead
    with keep_selection(s), applied(qtbot, s):
        s.edit.set_value(NodeRef(30, ("s",)), '"Ўзбекистон"')
    qtbot.waitUntil(lambda: v.edited_note_shown(), timeout=TIMEOUT)
    assert v.area.node_span() is None
    moves = v.area.moves
    # undo: the record is the file's again -- highlighted, without jumping
    with keep_selection(s), applied(qtbot, s):
        s.edit.undo()
    qtbot.waitUntil(lambda: v.area.node_span() == (node.start, node.end), timeout=TIMEOUT)
    assert not v.edited_note_shown()
    assert v.area.moves == moves
    # a new record (selected by the session after the insert): the note
    with applied(qtbot, s):
        s.edit.insert(None, 0, '{"id": -1}')
    qtbot.waitUntil(lambda: s.current is not None and s.current.record == 0, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: v.edited_note_shown(), timeout=TIMEOUT)
    # another file node: highlighted again
    s.select(NodeRef(5, ("s",)))
    file_node = s.doc.pinned().record_node(5)
    qtbot.waitUntil(lambda: v.area.node_span() is not None and not v.edited_note_shown(), timeout=TIMEOUT)
    assert file_node.start <= v.area.node_span()[0] < file_node.end
    assert v.area.size == path.stat().st_size                      # the original file


def test_raw_view_note_for_a_node_selected_while_hidden(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "a.json", [{"id": i} for i in range(5)]))
    with applied(qtbot, s):
        s.edit.set_value(NodeRef(2, ("id",)), "20")
    v = views(RawView, s)
    s.select(NodeRef(2, ("id",)))
    qtbot.waitUntil(lambda: v.edited_note_shown(), timeout=TIMEOUT)
    assert v.area.node_span() is None


# ----------------------------------------------------------------------------------
# schema view
# ----------------------------------------------------------------------------------
def field_texts(view: SchemaView) -> list[str]:
    return [item.fi.text for item in view.items()]


def test_schema_is_stale_after_edits_and_recomputed(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "a.json", [{"id": i, "name": f"n{i}"} for i in range(30)]))
    view = views(SchemaView, s)
    qtbot.waitUntil(lambda: view.schema is not None and not view.filling, timeout=TIMEOUT)
    assert "yangi" not in field_texts(view)
    with applied(qtbot, s):
        s.edit.set_path(NodeRef(3, ("yangi",)), '{"ichki": 1.10}')
    assert view.stale()
    assert "tahrirlardan" in view.header_label.text() or "after the edits" in view.header_label.text()
    qtbot.waitUntil(lambda: not view.stale() and not view.filling and "yangi" in field_texts(view), timeout=TIMEOUT)
    assert "yangi.ichki" in field_texts(view)
    fi = view.schema.find("yangi.ichki")
    assert fi.count == 1 and fi.types == {"number": 1}


def test_hidden_schema_view_recomputes_when_activated(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "a.json", [{"id": i} for i in range(10)]))
    view = views(SchemaView, s)
    qtbot.waitUntil(lambda: view.schema is not None and not view.filling, timeout=TIMEOUT)
    view.hide()
    with applied(qtbot, s):
        s.edit.delete([NodeRef(i, ()) for i in range(5)])
    with applied(qtbot, s):
        s.edit.set_path(NodeRef(0, ("extra",)), '"x"')
    qtbot.wait(EDIT_WAIT_MS)
    assert view.stale() and "extra" not in field_texts(view)        # hidden: nothing computed
    view.show()
    view.activate()
    qtbot.waitUntil(lambda: not view.stale() and not view.filling and "extra" in field_texts(view), timeout=TIMEOUT)
    assert view.schema.scanned == 5


EDIT_WAIT_MS = 600


def test_full_scan_after_edits(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "a.json", [{"id": i} for i in range(20)]))
    view = views(SchemaView, s)
    qtbot.waitUntil(lambda: view.schema is not None and not view.filling, timeout=TIMEOUT)
    view.full_scan()
    qtbot.waitUntil(lambda: view.schema is not None and view.schema.complete and not view.full_scan_running,
                    timeout=TIMEOUT)
    assert not view.full_button.isEnabled()                         # complete: nothing to scan
    with applied(qtbot, s):
        s.edit.set_path(NodeRef(7, ("flag",)), "true")
    assert view.full_button.isEnabled()                             # stale: the full scan applies again
    qtbot.waitUntil(lambda: not view.stale() and "flag" in field_texts(view), timeout=TIMEOUT)
    view.full_scan()
    qtbot.waitUntil(lambda: view.schema is not None and view.schema.complete and not view.full_scan_running
                    and "flag" in field_texts(view), timeout=TIMEOUT)
    assert view.schema.find("flag").types == {"bool": 1}


# ----------------------------------------------------------------------------------
# node readers (widgets)
# ----------------------------------------------------------------------------------
def test_task_doc_and_task_node_pin_one_state(qtbot, opened, tmp_path) -> None:
    s = opened(write_json(tmp_path / "a.json", [{"id": i} for i in range(3)]))
    doc = widgets.task_doc(s)
    assert isinstance(doc, PinnedDocument) and doc.gen == s.data_gen
    node = widgets.task_node(s, NodeRef(1, ("id",)))
    assert isinstance(node.doc, PinnedDocument) and node.value() == 1
    with applied(qtbot, s):
        s.edit.set_value(NodeRef(1, ("id",)), "10")
    assert doc.record(1) == {"id": 1}                              # the pinned state does not move
    node = widgets.task_node(s, NodeRef(1, ("id",)))
    assert node.value() == 10 and widgets.edited_span(node.start)
    assert widgets.edited_span(EDIT_BASE) and not widgets.edited_span(0) and not widgets.edited_span(None)


def test_resolve_in_and_remap_node_ref(qtbot, opened, tmp_path) -> None:
    s = opened(write(tmp_path / "a.json", '[{"a": [1, 2, 3], "k": 1, "k": 2}, {"a": []}]'))
    doc = widgets.task_doc(s)
    assert widgets.resolve_in(doc, NodeRef(0, ("a", "2"))) == NodeRef(0, ("a", 2), (0, 2))
    assert widgets.resolve_in(doc, NodeRef(0, ("k",))) == NodeRef(0, ("k",), (2,))     # the last duplicate
    assert widgets.resolve_in(doc, NodeRef(0, ("zz",))) is None
    assert widgets.resolve_in(doc, NodeRef(9, ())) is None
    with applied(qtbot, s) as got:
        s.edit.delete([NodeRef(0, ("a", 0), (0, 0))])
    cs = got[0]
    assert widgets.remap_node_ref(s, NodeRef(0, ("a", 2), (0, 2)), cs) == NodeRef(0, ("a", 1), (0, 1))
    assert widgets.remap_node_ref(s, NodeRef(0, ("a", 0), (0, 0)), cs) is None
    with applied(qtbot, s) as got:
        s.edit.insert(None, 0, "{}")
    assert widgets.remap_node_ref(s, NodeRef(1, ("a",)), got[0]) == NodeRef(2, ("a",))


def test_copy_value_of_edited_and_composite_nodes(qtbot, opened, tmp_path) -> None:
    s = opened(write(tmp_path / "a.json", '[{"id": 0, "n": 1.10}, {"id": 1, "n": 1e400}]'))
    with applied(qtbot, s):
        s.edit.set_value(NodeRef(1, ("n",)), "12345678901234567890")
    with applied(qtbot, s):
        s.edit.insert(None, 2, '{"id": 2, "s": "Ўзбекистон"}')
    QGuiApplication.clipboard().setText("")
    widgets.copy_node_value(s, NodeRef(1, ()), "json_compact")
    qtbot.waitUntil(lambda: clipboard() != "", timeout=TIMEOUT)
    assert json.loads(clipboard()) == {"id": 1, "n": 12345678901234567890}
    QGuiApplication.clipboard().setText("")
    widgets.copy_node_value(s, NodeRef(None, ()), "json_compact")   # the root: the effective records
    qtbot.waitUntil(lambda: clipboard() != "", timeout=TIMEOUT)
    assert clipboard().startswith('[{"id":0,"n":1.10}') and "12345678901234567890" in clipboard()
    assert "Ўзбекистон" in clipboard()
    full = widgets.load_full_text(s, NodeRef(None, ()))
    assert not full.truncated and json.loads(full.text) == [
        {"id": 0, "n": 1.1}, {"id": 1, "n": 12345678901234567890}, {"id": 2, "s": "Ўзбекистон"}]
    assert '"n": 1.10' in full.text
    part = widgets.load_full_text(s, NodeRef(None, ()), cap=20)
    assert part.truncated and len(part.text.encode()) <= 20


def test_full_text_of_the_wrapper_and_the_jsonl_root(qtbot, opened, tmp_path) -> None:
    s = opened(write(tmp_path / "w.json", '{"meta": {"v": 1}, "data": [{"id": 0}, {"id": 1}], "end": true}'),
               format="wrapped", record_path="data")
    with applied(qtbot, s):
        s.edit.delete([NodeRef(0, ())])
    with applied(qtbot, s):
        s.edit.set_value(NodeRef(None, ("meta", "v")), "-0.0")
    full = widgets.load_full_text(s, NodeRef(None, ()))
    assert json.loads(full.text.replace("-0.0", "0")) == {"meta": {"v": 0}, "data": [{"id": 1}], "end": True}
    assert '"v": -0.0' in full.text
    chunks = b"".join(widgets.node_text_chunks(widgets.task_node(s, NodeRef(None, ("data",)))))
    assert json.loads(chunks) == [{"id": 1}]
    j = opened(write(tmp_path / "d.jsonl", '{"id": 0}\n{"id": 1}\n'))
    with applied(qtbot, j):
        j.edit.insert(None, 2, '{"id": 2, "x": 1e400}')
    full = widgets.load_full_text(j, NodeRef(None, ()))
    assert "1e400" in full.text and full.text.count('"id"') == 3


def test_run_value_task_reads_the_effective_value(qtbot, opened, tmp_path) -> None:
    s = opened(write_json(tmp_path / "a.json", [{"o": {"k": [1, 2]}}]))
    with applied(qtbot, s):
        s.edit.set_value(NodeRef(0, ("o", "k")), '["Тошкент", 1.10]')
    got: list = []
    widgets.run_value_task(s, NodeRef(0, ("o",)), lambda value, cancel: codec.dumps(value).decode(), got.append)
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    assert got[0] == '{"k":["Тошкент",1.10]}'
    assert records(s)[0]["o"]["k"][0] == "Тошкент"
    fields.forget(s)


def test_schema_view_opened_after_edits_does_not_show_a_stale_cache(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "a.json", [{"id": i} for i in range(10)]))
    got: list = []
    fields.get_schema(s, got.append)                                # computed for another view (gen A)
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    with applied(qtbot, s):
        s.edit.set_path(NodeRef(2, ("later",)), "[1]")
    assert fields.is_stale(s)
    view = views(SchemaView, s)
    qtbot.waitUntil(lambda: view.schema is not None and not view.filling and not view.stale(), timeout=TIMEOUT)
    assert "later" in field_texts(view)
    assert fields.schema_gen(s) == s.data_gen
