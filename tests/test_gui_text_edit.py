"""The text view on edited documents (jsonreshape/gui/textview.py; ARCHITECTURE §12.9 "Text").

``build_pretty`` reads the effective text through ``doc.node_chunks`` (edited records, the root of an edited array,
the wrapper of an edited wrapped document); the shared cache key holds ``session.data_gen``; the view reloads its node
on ``data_changed`` when that node or its record changed -- keeping the first shown line, the caret and the folds of
the unchanged lines (:func:`textview.line_mapping`, checked against a naive model) --, remaps its node when records
move, shows a notice when it was deleted; "edit as JSON" (Shift+Enter, menu, toolbar button) round trips exact numbers
and Unicode through ``JsonEditDialog``; "open in raw view" is not offered for edited nodes.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402

from jsonreshape.gui import editors, textview  # noqa: E402
from jsonreshape.gui.session import NodeRef  # noqa: E402
from jsonreshape.gui.textview import TextView, build_pretty, line_mapping, line_starts  # noqa: E402

from views_edit_helpers import (  # noqa: E402,F401
    TIMEOUT,
    GuiReads,
    app,
    applied,
    keep_selection,
    no_logged_errors,
    opened,
    record_text,
    records,
    views,
    write,
    write_json,
)

pytestmark = [pytest.mark.gui, pytest.mark.usefixtures("no_logged_errors")]


# ----------------------------------------------------------------------------------
# line_mapping (pure)
# ----------------------------------------------------------------------------------
def _check_mapping(old: str, new: str) -> None:
    lm = line_mapping(old, line_starts(old), new, line_starts(new))
    ol, nl = old.split("\n"), new.split("\n")
    assert lm.delta == len(nl) - len(ol)
    assert 0 <= lm.head <= lm.tail <= len(ol)
    for line in range(len(ol)):
        got = lm.map(line)
        if got is None:
            assert lm.head <= line < lm.tail
            assert lm.clamp(line) == lm.head
            continue
        assert 0 <= got < len(nl) and ol[line] == nl[got]
        if line < lm.head:
            assert got == line and ol[:line + 1] == nl[:line + 1]
        else:
            assert ol[line:] == nl[got:]


@settings(max_examples=400, deadline=None)
@given(st.lists(st.sampled_from(["{", "}", "  a", "  b: 1", "", "[", "]"]), min_size=1, max_size=12),
       st.integers(0, 12), st.integers(0, 3), st.lists(st.sampled_from(["x", "{", "}", ""]), max_size=4))
def test_line_mapping_against_a_naive_model(lines, at, cut, insert) -> None:
    old = "\n".join(lines)
    at = min(at, len(lines))
    new_lines = lines[:at] + insert + lines[min(len(lines), at + cut):]
    new = "\n".join(new_lines or [""])
    _check_mapping(old, new)
    _check_mapping(new, old)


def test_line_mapping_examples() -> None:
    old = "{\n  \"a\": 1,\n  \"b\": 2,\n  \"c\": 3\n}"
    new = "{\n  \"a\": {\n    \"x\": 1\n  },\n  \"b\": 2,\n  \"c\": 3\n}"
    lm = line_mapping(old, line_starts(old), new, line_starts(new))
    assert (lm.head, lm.tail, lm.delta) == (1, 2, 2)
    assert [lm.map(i) for i in range(5)] == [0, None, 4, 5, 6]
    same = line_mapping(old, line_starts(old), old, line_starts(old))
    assert [same.map(i) for i in range(5)] == list(range(5))
    big_old = "\n".join(f"line {i}" for i in range(200_000))
    big_new = big_old.replace("line 150000", "line 150000\nnew one")
    lm = line_mapping(big_old, line_starts(big_old), big_new, line_starts(big_new))
    assert lm.map(10) == 10 and lm.map(150_000) == 150_000 and lm.map(150_001) == 150_002
    assert lm.map(199_999) == 200_000 and (lm.head, lm.tail, lm.delta) == (150_001, 150_001, 1)


def test_common_prefix_and_suffix_cross_chunks(monkeypatch) -> None:
    monkeypatch.setattr(textview, "_CMP_CHUNK", 7)
    rng = random.Random(5)
    for _ in range(300):
        a = "".join(rng.choice("ab\n") for _ in range(rng.randint(0, 60)))
        b = list(a)
        if b and rng.random() < 0.8:
            b[rng.randrange(len(b))] = rng.choice("abc\n")
        b = "".join(b)
        p = 0
        while p < min(len(a), len(b)) and a[p] == b[p]:
            p += 1
        assert textview._common_prefix(a, b) == p
        limit = min(len(a), len(b)) - p
        q = 0
        while q < limit and a[len(a) - 1 - q] == b[len(b) - 1 - q]:
            q += 1
        assert textview._common_suffix(a, b, limit) == q


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
def wait_shown(qtbot, v: TextView) -> None:
    qtbot.waitUntil(lambda: not v.is_loading() and (v.result() is not None or v.message() is not None
                                                     or v.node_deleted()), timeout=TIMEOUT)


def show(qtbot, v: TextView, s, ref: NodeRef) -> None:
    s.select(ref)
    wait_shown(qtbot, v)
    assert v.result() is not None, v.message()


def big_record(i: int) -> dict:
    return {"id": i, "name": f"n{i}", "items": [{"k": j, "v": f"value {j}", "deep": {"a": j, "b": [j, j + 1]}}
                                                for j in range(40)], "tail": {"x": 1.10, "y": "Ўзбек"}}


def json_text(v: TextView) -> str:
    return v.result().copy_text


def editor_dialog(qtbot, v: TextView) -> editors.JsonEditDialog:
    qtbot.waitUntil(lambda: v.json_dialog() is not None, timeout=TIMEOUT)
    return v.json_dialog()


# ----------------------------------------------------------------------------------
# reload after edits
# ----------------------------------------------------------------------------------
def test_reload_keeps_the_position_and_the_folds(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "big.json", [big_record(i) for i in range(3)]))
    v = views(TextView, s, width=900, height=400)
    show(qtbot, v, s, NodeRef(1, ()))
    ed = v.editor
    lines = v.result().text.split("\n")
    fold_late = next(i for i, ln in enumerate(lines) if ln.strip() == '"deep": {' and i > 300)
    fold_early = next(i for i, ln in enumerate(lines) if ln.strip() == '"deep": {')
    assert ed.fold(fold_late) and ed.fold(fold_early)
    ed.set_goal(top=250, caret=260)
    qtbot.waitUntil(lambda: not ed.goal_pending(), timeout=TIMEOUT)
    top_text = lines[250]
    caret_text = lines[260]
    before = ed.position()
    # an edit early in the record adds 2 lines (a scalar becomes an object)
    with keep_selection(s), applied(qtbot, s) as got:
        s.edit.set_value(NodeRef(1, ("name",)), '{"first": "Ali", "last": "Valiyev"}')
    assert got[0].records == frozenset({1})
    wait_shown(qtbot, v)
    qtbot.waitUntil(lambda: not ed.goal_pending() and not ed.busy(), timeout=TIMEOUT)
    new_lines = v.result().text.split("\n")
    assert '"first": "Ali",' in v.result().text
    first, caret = ed.position()
    assert new_lines[first] == top_text and first == before[0] + 3   # (1 line became 4)
    assert new_lines[caret] == caret_text
    assert ed.is_folded(fold_late + 3)                             # unchanged region: still folded, shifted
    assert ed.is_folded(fold_early + 3)
    assert len(ed.folded) == 2


def test_reload_only_when_the_shown_node_changed(qtbot, opened, views, tmp_path, monkeypatch) -> None:
    s = opened(write_json(tmp_path / "a.json", [{"id": i, "s": f"v{i}", "o": {"k": i}} for i in range(5)]))
    v = views(TextView, s)
    show(qtbot, v, s, NodeRef(2, ("o",)))
    builds: list = []
    real = textview.build_pretty
    monkeypatch.setattr(textview, "build_pretty", lambda *a, **k: builds.append(a[1]) or real(*a, **k))
    with keep_selection(s), applied(qtbot, s):  # another record: no rebuild
        s.edit.set_value(NodeRef(4, ("s",)), '"x"')
    qtbot.wait(50)
    assert builds == [] and json.loads(json_text(v)) == {"k": 2}
    with keep_selection(s), applied(qtbot, s):  # a record inserted before it: remapped only
        s.edit.insert(None, 0, '{"id": -1}')
    qtbot.wait(50)
    assert builds == []
    assert v.shown_ref() == NodeRef(3, ("o",)) or v.shown_ref().record == 3
    assert "#3" in v.path_label.full_text()
    with keep_selection(s), applied(qtbot, s):  # its record changed: rebuilt
        s.edit.set_value(NodeRef(3, ("o", "k")), "12345678901234567890")
    wait_shown(qtbot, v)
    qtbot.waitUntil(lambda: builds != [], timeout=TIMEOUT)
    assert json_text(v) == '{\n  "k": 12345678901234567890\n}'


def test_hidden_view_reloads_when_activated(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "a.json", [{"id": i, "s": f"v{i}"} for i in range(3)]))
    v = views(TextView, s)
    show(qtbot, v, s, NodeRef(1, ()))
    v.hide()
    with keep_selection(s), applied(qtbot, s):
        s.edit.set_value(NodeRef(1, ("s",)), '"yangi"')
    assert '"v1"' in json_text(v)                                  # hidden: no I/O
    v.show()
    v.activate()
    qtbot.waitUntil(lambda: v.result() is not None and '"yangi"' in json_text(v), timeout=TIMEOUT)


def test_undo_redo_and_the_cache_key(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "a.json", [{"id": i, "s": f"v{i}"} for i in range(3)]))
    v = views(TextView, s)
    show(qtbot, v, s, NodeRef(0, ()))
    with keep_selection(s), applied(qtbot, s):
        s.edit.set_value(NodeRef(0, ("s",)), '"A"')
    qtbot.waitUntil(lambda: v.result() is not None and '"A"' in json_text(v), timeout=TIMEOUT)
    show(qtbot, v, s, NodeRef(1, ()))
    with keep_selection(s), applied(qtbot, s):
        s.edit.undo()
    show(qtbot, v, s, NodeRef(0, ()))                              # never the cached text of the old state
    assert '"v0"' in json_text(v)
    with keep_selection(s), applied(qtbot, s):
        s.edit.redo()
    qtbot.waitUntil(lambda: v.result() is not None and '"A"' in json_text(v), timeout=TIMEOUT)


def test_deleted_node_notice(qtbot, opened, views, tmp_path, monkeypatch) -> None:
    s = opened(write_json(tmp_path / "a.json", [{"id": i, "o": {"k": i}} for i in range(4)]))
    v = views(TextView, s)
    show(qtbot, v, s, NodeRef(2, ("o",)))
    with keep_selection(s), applied(qtbot, s):  # nobody selects another node
        s.edit.delete([NodeRef(2, ())])
    assert v.node_deleted() and v.shown_ref() is None
    assert v.editor.empty_text()
    assert v.edit_json_block() is not None


def test_deleted_node_follows_the_new_selection(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "a.json", [{"id": i, "o": {"k": i}} for i in range(4)]))
    v = views(TextView, s)
    show(qtbot, v, s, NodeRef(2, ()))
    with applied(qtbot, s):
        s.edit.delete([NodeRef(2, ())])
    # the session selects the neighbour (cs.select): the view shows it
    qtbot.waitUntil(lambda: v.shown_ref() is not None and v.shown_ref().record == 2 and v.result() is not None,
                    timeout=TIMEOUT)
    assert json.loads(json_text(v))["id"] == 3


# ----------------------------------------------------------------------------------
# composite nodes
# ----------------------------------------------------------------------------------
def test_root_of_an_edited_array(qtbot, opened, views, tmp_path) -> None:
    s = opened(write(tmp_path / "a.json", '[{"id": 0, "n": 1.10}, {"id": 1, "n": 1e400}, {"id": 2, "n": -0.0}]'))
    v = views(TextView, s)
    show(qtbot, v, s, NodeRef(None, ()))
    assert json.loads(json_text(v).replace("1e400", "0")) is not None
    with keep_selection(s), applied(qtbot, s):
        s.edit.set_value(NodeRef(1, ("n",)), "12345678901234567890")
    qtbot.waitUntil(lambda: v.result() is not None and "12345678901234567890" in json_text(v), timeout=TIMEOUT)
    with keep_selection(s), applied(qtbot, s):
        s.edit.insert(None, 3, '{"id": 3, "s": "Ўзбекистон O\'zbekiston"}')
    qtbot.waitUntil(lambda: v.result() is not None and "Ўзбекистон" in json_text(v), timeout=TIMEOUT)
    with keep_selection(s), applied(qtbot, s):
        s.edit.delete([NodeRef(0, ())])
    qtbot.waitUntil(lambda: v.result() is not None and '"id": 0' not in json_text(v), timeout=TIMEOUT)
    text = json_text(v)
    assert "1.10" not in text and "-0.0" in text and "12345678901234567890" in text
    expected = b"[" + b",".join(s.doc.pinned().record_bytes(i) for i in range(3)) + b"]"
    from jsonreshape.core import Reformatter

    f = Reformatter(indent=2)
    assert text == (f.feed(expected) + f.finish()).decode("utf-8")
    assert not v.result().edited and v.can_open_raw()              # the root: a span of the file


def test_wrapper_of_an_edited_wrapped_document(qtbot, opened, views, tmp_path) -> None:
    text = ('{"meta": {"v": 1, "who": "Ali"}, "data": [{"id": 0}, {"id": 1}], "tail": [1.10, 2]}')
    s = opened(write(tmp_path / "w.json", text), format="wrapped", record_path="data")
    v = views(TextView, s)
    show(qtbot, v, s, NodeRef(None, ()))
    assert json.loads(json_text(v))["data"] == [{"id": 0}, {"id": 1}]
    with keep_selection(s), applied(qtbot, s):  # a record: the wrapper's text changes
        s.edit.set_value(NodeRef(1, ("id",)), "1e400")
    qtbot.waitUntil(lambda: v.result() is not None and "1e400" in json_text(v), timeout=TIMEOUT)
    with keep_selection(s), applied(qtbot, s):  # a wrapper member
        s.edit.set_value(NodeRef(None, ("meta", "who")), '"Тошкент"')
    qtbot.waitUntil(lambda: v.result() is not None and "Тошкент" in json_text(v), timeout=TIMEOUT)
    assert '"tail": [\n    1.10,' in json_text(v)
    # the record container (an ancestor's child holding the records)
    show(qtbot, v, s, NodeRef(None, ("data",)))
    assert json.loads(json_text(v).replace("1e400", "7")) == [{"id": 0}, {"id": 7}]
    with keep_selection(s), applied(qtbot, s):
        s.edit.delete([NodeRef(0, ())])
    qtbot.waitUntil(lambda: v.result() is not None and '"id": 0' not in json_text(v), timeout=TIMEOUT)
    # a wrapper member outside the records: an edit of a record does not rebuild it
    show(qtbot, v, s, NodeRef(None, ("meta",)))
    assert "Тошкент" in json_text(v)


def test_jsonl_root_count_follows(qtbot, opened, views, tmp_path) -> None:
    s = opened(write(tmp_path / "d.jsonl", '{"id": 0}\n{"id": 1}\n'))
    v = views(TextView, s)
    s.select(NodeRef(None, ()))
    wait_shown(qtbot, v)
    assert v.result().virtual and v.result().records == 2
    assert v.edit_json_block() is not None and not v.edit_button.isEnabled()
    with keep_selection(s), applied(qtbot, s):
        s.edit.insert(None, 2, '{"id": 2}')
    qtbot.waitUntil(lambda: v.result() is not None and v.result().records == 3, timeout=TIMEOUT)


# ----------------------------------------------------------------------------------
# edit as JSON
# ----------------------------------------------------------------------------------
EXACT = '{"id": 7, "n": 1.10, "big": 1e400, "neg": -0.0, "huge": 12345678901234567890, "uz": "O\'zbekiston", ' \
        '"cyr": "Ўзбекистон 😀", "e": "\\u00e9\\n"}'


def test_edit_as_json_round_trip(qtbot, opened, views, tmp_path) -> None:
    s = opened(write(tmp_path / "a.json", "[" + EXACT + ', {"id": 8}]'))
    v = views(TextView, s)
    show(qtbot, v, s, NodeRef(0, ()))
    assert v.edit_button.isEnabled()
    v.editor.setFocus()
    qtbot.keyClick(v.editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    dlg = editor_dialog(qtbot, v)
    assert dlg.text() == json_text(v)                              # the pretty text shown, exact tokens
    new = json_text(v).replace('"id": 7', '"id": 70').replace('"neg": -0.0', '"neg": -0.0, "x": [1.10, 1e400]')
    with keep_selection(s), applied(qtbot, s):
        dlg.editor.setPlainText(new)
        dlg.accept()
    raw = record_text(s, 0)
    for token in ('"id": 70', "1.10", "1e400", "-0.0", "12345678901234567890", "O'zbekiston",
                  "Ўзбекистон 😀",
                  "\\u00e9\\n"):
        assert token in raw, token
    qtbot.waitUntil(lambda: v.result() is not None and '"id": 70' in json_text(v), timeout=TIMEOUT)
    assert records(s)[1] == {"id": 8}


def test_edit_as_json_from_the_menu_and_the_protocol(qtbot, opened, views, tmp_path) -> None:
    s = opened(write(tmp_path / "a.json", '[{"id": 1, "o": {"a": [1, 2]}}]'))
    v = views(TextView, s)
    show(qtbot, v, s, NodeRef(0, ("o",)))
    menu = v.build_context_menu()
    act = next(a for a in menu.actions() if a.objectName() == "text_edit_json")
    assert act.isEnabled()
    act.trigger()
    dlg = editor_dialog(qtbot, v)
    with keep_selection(s), applied(qtbot, s):
        dlg.editor.setPlainText('{"a": [], "b": null}')
        dlg.accept()
    assert records(s)[0]["o"] == {"a": [], "b": None}
    qtbot.waitUntil(lambda: v.result() is not None and '"b": null' in json_text(v), timeout=TIMEOUT)
    assert v.edit_command_state("edit_value") == (True, "")
    assert v.edit_command_state("delete") is None
    v.edit_value()
    dlg = editor_dialog(qtbot, v)
    dlg.discard()


def test_edit_as_json_refusals(qtbot, opened, views, tmp_path, monkeypatch) -> None:
    s = opened(write_json(tmp_path / "a.json", [{"id": 1, "s": "x" * 400}], indent=None))
    v = views(TextView, s)
    monkeypatch.setattr(textview, "TEXT_CAP", 200)
    show(qtbot, v, s, NodeRef(0, ()))
    assert v.result().truncated
    assert v.edit_json_block() is not None and not v.edit_button.isEnabled()
    n = len(s.app.statuses)
    assert v.edit_as_json() is None and len(s.app.statuses) > n
    monkeypatch.setattr(textview, "TEXT_CAP", 4 * 1024 * 1024)
    show(qtbot, v, s, NodeRef(0, ("id",)))
    assert v.edit_json_block() is None
    monkeypatch.setattr(type(s.edit), "available", property(lambda self: False))
    assert v.edit_json_block()                                     # the controller's reason text


def test_dialog_edits_the_remapped_node(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "a.json", [{"id": i, "o": {"k": i}} for i in range(3)]))
    v = views(TextView, s)
    show(qtbot, v, s, NodeRef(1, ("o",)))
    v.edit_as_json()
    dlg = editor_dialog(qtbot, v)
    with keep_selection(s), applied(qtbot, s):  # a record inserted before it
        s.edit.insert(None, 0, '{"id": -1}')
    with keep_selection(s), applied(qtbot, s):
        dlg.editor.setPlainText('{"k": "moved"}')
        dlg.accept()
    assert records(s)[2] == {"id": 1, "o": {"k": "moved"}}


# ----------------------------------------------------------------------------------
# raw view links
# ----------------------------------------------------------------------------------
def test_open_raw_is_not_offered_for_edited_nodes(qtbot, opened, views, tmp_path, monkeypatch) -> None:
    s = opened(write_json(tmp_path / "a.json", [{"id": i, "s": "x" * 300} for i in range(3)], indent=None))
    v = views(TextView, s)
    show(qtbot, v, s, NodeRef(1, ()))
    assert v.can_open_raw()
    with keep_selection(s), applied(qtbot, s):
        s.edit.set_value(NodeRef(1, ("id",)), "99")
    qtbot.waitUntil(lambda: v.result() is not None and '"id": 99' in json_text(v), timeout=TIMEOUT)
    assert v.result().edited and not v.can_open_raw()
    assert "tahrirlangan" in v.size_label.text() or "edited" in v.size_label.text()
    emitted: list = []
    v.open_raw_requested.connect(lambda *a: emitted.append(a))
    v.request_raw()
    assert emitted == []
    monkeypatch.setattr(textview, "TEXT_CAP", 100)                 # a cut text: no "open raw" button
    show(qtbot, v, s, NodeRef(2, ()))
    v.request_raw()
    assert emitted and emitted[-1][1] == v.result().raw_size
    with keep_selection(s), applied(qtbot, s):
        s.edit.set_value(NodeRef(2, ("id",)), "98")
    qtbot.waitUntil(lambda: v.result() is not None and v.result().edited, timeout=TIMEOUT)
    assert v.result().truncated
    labels = [b.text() for b in v.info_bar.buttons()]
    from jsonreshape.i18n import t

    assert t("gui.text.open_raw") not in labels


def test_build_pretty_reads_no_file_bytes_in_the_gui_thread(qtbot, opened, views, tmp_path, monkeypatch) -> None:
    probe = GuiReads(monkeypatch)
    s = opened(write_json(tmp_path / "a.json", [{"id": i, "s": "x" * 70_000} for i in range(3)], indent=None))
    v = views(TextView, s)
    show(qtbot, v, s, NodeRef(1, ()))
    with keep_selection(s), applied(qtbot, s):
        s.edit.set_value(NodeRef(1, ("id",)), "5")
    qtbot.waitUntil(lambda: v.result() is not None and '"id": 5' in json_text(v), timeout=TIMEOUT)
    show(qtbot, v, s, NodeRef(None, ()))
    assert probe.big() == []
    # (build_pretty itself runs in workers; direct call here for the pinned-state rule)
    pretty = build_pretty(s, NodeRef(1, ()), 2)
    assert '"id": 5' in pretty.text and pretty.edited


def test_edit_as_json_keeps_the_position_when_the_caret_selected_a_child(qtbot, opened, views, tmp_path) -> None:
    s = opened(write_json(tmp_path / "big.json", [big_record(i) for i in range(2)]))
    v = views(TextView, s, width=900, height=400)
    show(qtbot, v, s, NodeRef(1, ()))
    ed = v.editor
    lines = v.result().text.split("\n")
    ed.set_goal(top=200, caret=210)
    qtbot.waitUntil(lambda: not ed.goal_pending(), timeout=TIMEOUT)
    v._select_at_caret()                                           # (the debounced caret selection, now)
    qtbot.waitUntil(lambda: s.current is not None and s.current.path != (), timeout=TIMEOUT)
    top_text, caret_text = lines[200], lines[210]
    v.edit_as_json()
    dlg = editor_dialog(qtbot, v)
    new = dlg.text().replace('"name": "n1"', '"name": {\n    "a": 1,\n    "b": 2\n  }', 1)
    with applied(qtbot, s):
        dlg.editor.setPlainText(new)
        dlg.accept()
    # the session selects the edited node (the record): the view keeps it, the text moved by 3 lines
    qtbot.waitUntil(lambda: v.result() is not None and '"a": 1' in json_text(v) and not ed.goal_pending()
                    and not ed.busy(), timeout=TIMEOUT)
    first, caret = ed.position()
    new_lines = v.result().text.split("\n")
    assert new_lines[first] == top_text and new_lines[caret] == caret_text
    assert first == 203
    assert s.current.record == 1 and s.current.path == ()
