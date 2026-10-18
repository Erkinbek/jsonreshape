"""FilterPanel (jsonreshape/gui/filterpanel.py; ARCHITECTURE §11.8, AGENT_PROMPT §6.3, §14 test 18).

Real sessions on generated files: the visual builder and the equivalent typed expression
select exactly the same records (and the records a plain evaluation selects) for many
operators -- between, in, len, exists / missing, regex, null, empty, startswith, contains --
with nested OR / NOT groups; JMESPath; the live syntax check (error position underlined);
text -> builder sync and the read-only builder for expressions it cannot show; builder
errors highlighting their row; value typing (number vs string badge, forced strings, ``in``
lists); presets (save / overwrite / load / delete, prompt, busy errors); one draft per
session across ``set_session`` switches; following filters changed elsewhere; single
documents and no file; ``add_condition`` / Enter / Ctrl+Enter; a larger file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402

from jsonreshape import codec  # noqa: E402
from jsonreshape.errors import JsonReshapeError  # noqa: E402
from jsonreshape.gui import filterpanel as fp  # noqa: E402
from jsonreshape.gui import widgets  # noqa: E402
from jsonreshape.gui.context import AppContext  # noqa: E402
from jsonreshape.gui.filterpanel import FilterPanel, display_value, interpret_value  # noqa: E402
from jsonreshape.gui.guistate import GuiState  # noqa: E402
from jsonreshape.gui.session import DocSession, FilterSpec, NodeRef  # noqa: E402
from jsonreshape.query import Condition, FilterPresets, Group, compile_filter  # noqa: E402

pytestmark = pytest.mark.gui

TIMEOUT = 30000
CITIES = ["Toshkent", "Samarqand", "Buxoro", "Xiva"]


def make_records(n: int) -> list[dict]:
    out = []
    for i in range(n):
        r = {"id": i, "name": f"{['Ali', 'Vali', 'Gani', 'Olim'][i % 4]}{i % 7}",
             "age": None if i % 11 == 0 else i % 70,
             "contact": {"city": CITIES[i % 4], "zip": str(100000 + i % 50)},
             "tags": [["vip"], [], ["x", "vip"], ["x"]][i % 4],
             "children": list(range(i % 5))}
        if i % 3:
            r["email"] = f"u{i}@mail.uz"
        if i % 13 == 0:
            r["note"] = ""
        out.append(r)
    return out


RECS = make_records(400)


# ----------------------------------------------------------------------------------
# fixtures and helpers
# ----------------------------------------------------------------------------------
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
    made: list = []

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
def array_file(tmp_path: Path) -> Path:
    path = tmp_path / "people.json"
    path.write_text(json.dumps(RECS, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def panel(qtbot, app) -> FilterPanel:
    p = FilterPanel(app)
    qtbot.addWidget(p)
    p.resize(700, 600)
    return p


@pytest.fixture
def confirm_answer(monkeypatch):
    answers: list[bool] = []
    asked: list[str] = []

    def handler(parent, text, title):
        asked.append(text)
        return answers.pop(0) if answers else True

    monkeypatch.setattr(widgets, "confirm_handler", handler)
    return answers, asked


def view_records(s: DocSession) -> list[int]:
    return [s.record_at(i) for i in range(s.view_len())]


def apply_and_wait(qtbot, panel: FilterPanel) -> list[int]:
    s = panel.session
    assert panel.apply()
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    return view_records(s)


def expected(text: str, lang: str = "expr", recs: list | None = None) -> list[int]:
    flt = compile_filter(text, lang)
    out = []
    for i, r in enumerate(recs if recs is not None else RECS):
        try:
            if flt.matches(r, None, i):
                out.append(i)
        except JsonReshapeError:
            pass
    return out


def add_row(group, field: str, op: str, *raws: str, force: bool = False):
    row = group.add_condition()
    row.field.setEditText(field)
    row.set_op(op)
    for edit, raw in zip((row.v1, row.v2), raws):
        edit.set_raw(raw, force)
    return row


# ----------------------------------------------------------------------------------
# value typing (pure)
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize(("raw", "op", "force", "want"), [
    ("12", "eq", False, (True, 12)),
    ("12", "eq", True, (True, "12")),
    ('"12"', "eq", False, (True, "12")),
    ("Toshkent", "eq", False, (True, "Toshkent")),
    ("true", "ne", False, (True, True)),
    ("null", "eq", False, (True, None)),
    ("[1, 2]", "eq", False, (True, [1, 2])),
    ("", "eq", False, (False, None)),
    ("   ", "eq", False, (False, None)),
    ("", "eq", True, (True, "")),
    ("12", "regex", False, (True, "12")),
    ("^a.*", "regex", False, (True, "^a.*")),
    ('"a b"', "regex", False, (True, "a b")),
    ("Toshkent, Samarqand", "in", False, (True, ["Toshkent", "Samarqand"])),
    ("1, 2, x", "in", False, (True, [1, 2, "x"])),
    ("1, 2", "in", True, (True, ["1", "2"])),
    ('["a", 1]', "in", False, (True, ["a", 1])),
    ("5", "in", False, (True, [5])),
    ('"abc"', "in", False, (True, "abc")),
    ("", "in", False, (False, None)),
    ("3", "len_gt", False, (True, 3)),
])
def test_interpret_value(raw: str, op: str, force: bool, want: tuple) -> None:
    assert interpret_value(raw, op, force) == want


@pytest.mark.parametrize(("value", "op"), [
    ("Toshkent", "eq"), ("12", "eq"), ("", "eq"), ("true", "eq"), (12, "eq"), (None, "eq"), ([1, "a"], "in"),
    ("abc", "in"), ("a,b", "in"), ("^x", "regex"), ('"q"', "eq"), (codec.loads("1.10"), "gt"), ({"a": 1}, "eq"),
])
def test_display_value_reads_back(value, op: str) -> None:
    raw = display_value(value, op)
    ok, back = interpret_value(raw, op)
    assert ok and codec.dumps(back) == codec.dumps(value), (raw, back)


# ----------------------------------------------------------------------------------
# builder == expression (AGENT_PROMPT §14 test 18)
# ----------------------------------------------------------------------------------
def test_builder_and_typed_expression_select_the_same_records(qtbot, panel, opened, array_file, app) -> None:
    s = opened(array_file)
    panel.set_session(s)
    ed = panel.current_editor()
    root = ed.root

    # 1) AND of a number comparison, a nested string field, between and a list
    add_row(root, "age", "gt", "30")
    add_row(root, "contact.city", "eq", "Toshkent")
    assert ed.text() == 'age > 30 and contact.city == "Toshkent"'
    built = apply_and_wait(qtbot, panel)
    assert built == expected('age > 30 and contact.city = "Toshkent"') and built
    assert s.filter.builder == Group("and", [Condition("age", "gt", 30),
                                             Condition("contact.city", "eq", "Toshkent")]).to_dict()
    ed.set_text("contact.city == 'Toshkent' && age > 30")
    assert apply_and_wait(qtbot, panel) == built

    # 2) OR with a negated sub-group: startswith / contains / len / regex / in / between
    panel.clear()
    qtbot.waitUntil(lambda: not s.view_busy and s.filter is None, timeout=TIMEOUT)
    root.set_op("or")
    add_row(root, "name", "startswith", "Ali")
    sub = root.add_group()
    sub.negate.setChecked(True)
    add_row(sub, "tags", "contains", "vip")
    add_row(sub, "children", "len_ge", "2")
    add_row(root, "email", "regex", r"^u1\d*@")
    add_row(root, "contact.zip", "in", '"100001", "100002"')
    add_row(root, "id", "between", "390", "395")
    want_text = ('name startswith "Ali" or not (tags contains "vip" and len(children) >= 2) or '
                 'email matches "^u1\\\\d*@" or contact.zip in ["100001","100002"] or id between 390 and 395')
    assert ed.text() == want_text
    built = apply_and_wait(qtbot, panel)
    typed = ("name startswith 'Ali' || !(tags contains 'vip' && len(children) >= 2) || email ~ '^u1\\\\d*@'"
             " || contact.zip in ['100001', '100002'] || id between 395 and 390")
    assert built == expected(typed)
    ed.set_text(typed)
    assert ed.builder_available()      # representable: the builder follows the text
    assert apply_and_wait(qtbot, panel) == built

    # 3) value-less operators and $index
    panel.clear()
    qtbot.waitUntil(lambda: not s.view_busy and s.filter is None, timeout=TIMEOUT)
    add_row(root, "email", "not_exists")
    add_row(root, "age", "is_null")
    add_row(root, "$index", "lt", "200")
    assert ed.text() == "missing(email) and age is null and $index < 200"
    built = apply_and_wait(qtbot, panel)
    assert built == expected("email is missing and age is null and $index < 200") and built
    ed.set_text("not exists(email) and age is null and $index <= 199")
    assert apply_and_wait(qtbot, panel) == built

    # 4) exists + is_empty + len_eq + ne
    panel.clear()
    qtbot.waitUntil(lambda: not s.view_busy and s.filter is None, timeout=TIMEOUT)
    add_row(root, "note", "exists")
    add_row(root, "note", "is_empty")
    add_row(root, "tags", "len_eq", "0")
    add_row(root, "contact.city", "ne", "Xiva")
    built = apply_and_wait(qtbot, panel)
    assert built == expected('exists(note) and note is empty and len(tags) == 0 and contact.city ≠ "Xiva"')
    assert app.errors == []


def test_jmespath_filter(qtbot, panel, opened, array_file, lang_uz) -> None:
    s = opened(array_file)
    panel.set_session(s)
    ed = panel.current_editor()
    text = "age > `30` && contact.city == 'Samarqand'"
    ed.set_text(text, "jmespath")
    assert ed.state == "jmespath" and not ed.builder_available()
    assert ed.notice_text() and "JMESPath" in ed.notice_text()
    assert not ed.scroll.isEnabled()
    got = apply_and_wait(qtbot, panel)
    assert got == expected(text, "jmespath") and got
    assert s.filter == FilterSpec(text, "jmespath", None)
    assert "Natija: 400 ta yozuvdan" in panel.status_text()


def test_jmespath_runtime_warnings_are_shown(qtbot, panel, opened, array_file, lang_uz) -> None:
    s = opened(array_file)
    panel.set_session(s)
    panel.current_editor().set_text("abs(age) > `60`", "jmespath")    # null ages cannot be evaluated
    got = apply_and_wait(qtbot, panel)
    assert got == expected("age > 60")
    assert s.view_warnings and panel.warning_text() == "\n".join(s.view_warnings)


# ----------------------------------------------------------------------------------
# expression line: live syntax check, sync with the builder
# ----------------------------------------------------------------------------------
def test_live_syntax_error_position(qtbot, panel, opened, array_file, lang_uz) -> None:
    s = opened(array_file)
    panel.set_session(s)
    panel.show()
    ed = panel.current_editor()
    ed.expr.setFocus()
    qtbot.keyClicks(ed.expr, "age > 30 and (")
    qtbot.waitUntil(lambda: ed.error() is not None, timeout=5000)     # debounced check
    assert ed.error().pos == 14 and ed.expr.error_pos() == 14
    sels = ed.expr.extraSelections()
    assert len(sels) == 1 and sels[0].cursor.selectedText() == "("      # the end: last character marked
    assert ed.error_label.text().startswith("15-belgida xato:")
    assert ed.state == "invalid" and not ed.scroll.isEnabled()
    assert not panel.apply() and s.filter is None
    assert "xato" in panel.status_text()
    qtbot.keyClicks(ed.expr, "name ~ '[')")
    qtbot.waitUntil(lambda: ed.error() is not None and ed.error().pos == 22, timeout=5000)
    assert ed.expr.extraSelections()[0].cursor.selectedText() == "["   # inside the regex literal
    ed.set_text("age > 30 and (name ~ '[a]')")
    assert ed.error() is None and ed.error_label.isHidden() and ed.expr.extraSelections() == []
    assert ed.builder_available()
    ed.set_text("age > `30", "jmespath")
    assert ed.error() is not None and ed.error().pos == 6


def test_text_rebuilds_builder_and_unrepresentable_text(qtbot, panel, opened, array_file, lang_uz) -> None:
    s = opened(array_file)
    panel.set_session(s)
    ed = panel.current_editor()
    ed.set_text("age > 3 and (contact.city == 'Xiva' or not (email is null))")
    g = ed.builder_group()
    assert g == Group("and", [Condition("age", "gt", 3),
                              Group("or", [Condition("contact.city", "eq", "Xiva"),
                                           Group("and", [Condition("email", "is_null")], True)])])
    rows = ed.root.rows()
    assert [r.field_text() for r in rows] == ["age", "contact.city", "email"]
    assert rows[0].v1.raw() == "3" and rows[0].v1.type_name() == "number"
    assert rows[1].v1.raw() == "Xiva" and rows[1].v1.type_name() == "string"
    assert ed.root.groups()[1].negate.isChecked()
    # typing in the expression line keeps the text as typed (authoritative)
    ed.set_text("age is not null")
    assert ed.text() == "age is not null" and ed.builder_group() == Group(items=[Condition("age", "is_null")],
                                                                          negate=True)
    # not representable: the builder becomes read-only, the text still applies
    ed.set_text("lower(name) == 'ali1' or age == id")
    assert ed.state == "unrepresentable" and ed.builder_group() is None
    assert "quruvchida ko'rsatib bo'lmaydi" in ed.notice_text()
    assert not ed.scroll.isEnabled()
    got = apply_and_wait(qtbot, panel)
    assert got == expected("lower(name) == 'ali1' or age == id") and got
    assert s.filter.builder is None
    # an unrepresentable text: add_condition ANDs to the text
    assert panel.add_condition("age", "gt", 50)
    assert ed.text() == "(lower(name) == 'ali1' or age == id) and age > 50"
    # clearing gives the builder back
    panel.clear()
    assert ed.builder_available() and ed.text() == "" and ed.scroll.isEnabled()
    qtbot.waitUntil(lambda: not s.view_busy and s.filter is None, timeout=TIMEOUT)
    assert s.view_len() == len(RECS)


def test_builder_edits_and_value_badges(qtbot, panel, opened, array_file) -> None:
    s = opened(array_file)
    panel.set_session(s)
    ed = panel.current_editor()
    row = add_row(ed.root, "contact.zip", "eq", "100007")
    assert ed.text() == "contact.zip == 100007" and row.v1.type_name() == "number"
    assert row.v1.badge.text() == "123"
    row.v1.force.setChecked(True)
    assert ed.text() == 'contact.zip == "100007"' and row.v1.type_name() == "string"
    assert row.v1.badge.text() == "abc"
    got = apply_and_wait(qtbot, panel)
    assert got == [i for i, r in enumerate(RECS) if r["contact"]["zip"] == "100007"] and got
    # operator change: between shows the second value, a value-less operator hides both
    row.set_op("between")
    assert row.v2.isVisibleTo(row) and ed.text() == ""      # incomplete: no second value yet
    row.v2.set_raw("100009", force_string=True)
    assert ed.text() == 'contact.zip between "100007" and "100009"'
    row.set_op("exists")
    assert not row.v1.isVisibleTo(row) and ed.text() == "exists(contact.zip)"
    # an empty field is incomplete; removing rows / groups
    empty = ed.root.add_condition()
    assert ed.text() == "exists(contact.zip)"
    ed.root.remove_item(empty)
    grp = ed.root.add_group(op="or")
    add_row(grp, "age", "lt", "5")
    add_row(grp, "age", "gt", "65")
    assert ed.text() == "exists(contact.zip) and (age < 5 or age > 65)"
    grp.remove_button.click()
    assert ed.text() == "exists(contact.zip)"
    row.remove_button.click()
    assert ed.text() == "" and ed.root.empty_hint.isVisibleTo(ed.root)


def test_builder_errors_highlight_the_row(qtbot, panel, opened, array_file, lang_uz) -> None:
    s = opened(array_file)
    panel.set_session(s)
    ed = panel.current_editor()
    good = add_row(ed.root, "age", "gt", "3")
    grp = ed.root.add_group()
    bad = add_row(grp, "a b", "eq", "1")              # not a valid path
    assert ed.error_rows() == [bad] and not good.has_error()
    assert ed.builder_error_text().startswith("Quruvchida xato:")
    assert ed.text() == "age > 3"                       # the text keeps the last valid state
    assert not panel.apply() and s.filter is None
    assert "qizil" in panel.status_text()
    bad.field.setEditText("a.b")
    assert ed.error_rows() == [] and ed.builder_error_text() is None
    assert "qizil" not in panel.status_text()          # the message about the old content is gone
    assert ed.text() == "age > 3 and (a.b == 1)"
    rx = add_row(grp, "name", "regex", "(")            # invalid pattern: found without an item index
    assert ed.error_rows() == [rx]
    rx.v1.set_raw("(a)")
    assert ed.error_rows() == [] and ed.text() == 'age > 3 and (a.b == 1 and name matches "(a)")'


# ----------------------------------------------------------------------------------
# presets
# ----------------------------------------------------------------------------------
def wait_presets(qtbot, panel: FilterPanel) -> None:
    """Presets are read / written in background tasks (fix round T9)."""
    qtbot.waitUntil(lambda: not panel.presets_busy(), timeout=TIMEOUT)


def test_presets_save_load_overwrite_delete(qtbot, panel, opened, array_file, monkeypatch, confirm_answer,
                                           _isolated_app_home, lang_uz) -> None:
    answers, asked = confirm_answer
    s = opened(array_file)
    panel.set_session(s)
    wait_presets(qtbot, panel)
    ed = panel.current_editor()
    ed.set_text("contact.city == 'Toshkent' and age > 30")
    assert panel.save_preset("Toshkentliklar")
    assert panel.presets_busy() and not panel.preset_save_button.isEnabled()   # in the background
    wait_presets(qtbot, panel)
    assert panel.preset_save_button.isEnabled()
    assert panel.preset_names() == ["Toshkentliklar"]
    assert (_isolated_app_home / "filter_presets.json").exists()
    assert FilterPresets().get("toshkentliklar")["text"] == "contact.city == 'Toshkent' and age > 30"
    # the prompt (QInputDialog) path
    ed.set_text("age > `60`", "jmespath")
    monkeypatch.setattr(fp.QInputDialog, "getText", lambda *a, **k: ("Keksalar", True))
    assert panel.save_preset()
    wait_presets(qtbot, panel)
    assert panel.preset_names() == ["Keksalar", "Toshkentliklar"]
    monkeypatch.setattr(fp.QInputDialog, "getText", lambda *a, **k: ("", False))
    assert not panel.save_preset()
    # overwrite asks first
    ed.set_text("age < 5")
    answers.append(False)
    assert not panel.save_preset("TOSHKENTLIKLAR")
    assert "Toshkentliklar" in asked[-1]
    assert FilterPresets().get("Toshkentliklar")["text"].startswith("contact.city")
    answers.append(True)
    assert panel.save_preset("TOSHKENTLIKLAR")
    wait_presets(qtbot, panel)
    assert FilterPresets().get("Toshkentliklar")["text"] == "age < 5"
    # load: text + language, the builder follows
    ed.set_text("")
    assert panel.load_preset("Keksalar")
    wait_presets(qtbot, panel)
    assert ed.text() == "age > `60`" and ed.lang_name() == "jmespath" and ed.state == "jmespath"
    assert panel.load_preset("TOSHKENTLIKLAR")
    wait_presets(qtbot, panel)
    assert ed.text() == "age < 5" and ed.builder_group() == Group(items=[Condition("age", "lt", 5)])
    assert apply_and_wait(qtbot, panel) == expected("age < 5")
    # delete with confirmation
    panel.preset_combo.setCurrentIndex(panel.preset_names().index("Keksalar"))
    answers.append(False)
    assert not panel.delete_preset()
    answers.append(True)
    assert panel.delete_preset()
    wait_presets(qtbot, panel)
    assert panel.preset_names() == ["TOSHKENTLIKLAR"]
    assert panel.load_preset("Keksalar")
    wait_presets(qtbot, panel)                          # gone: reported
    assert panel.app.errors and panel.app.errors[-1].key == "query.preset.not_found"


def test_preset_errors_are_reported(qtbot, panel, opened, array_file, monkeypatch) -> None:
    s = opened(array_file)
    panel.set_session(s)
    panel.current_editor().set_text("age > 1")

    def busy(self, name, text, lang="expr"):
        raise JsonReshapeError("query.preset.busy", path="x")

    monkeypatch.setattr(FilterPresets, "save", busy)
    assert panel.save_preset("x")
    wait_presets(qtbot, panel)
    assert panel.app.errors[-1].key == "query.preset.busy"
    monkeypatch.undo()
    panel.current_editor().set_text("")
    assert not panel.save_preset("empty")              # an empty expression is refused at once
    assert panel.app.errors[-1].key == "query.preset.empty"


# ----------------------------------------------------------------------------------
# sessions, drafts, external changes, availability
# ----------------------------------------------------------------------------------
def test_drafts_per_session(qtbot, panel, opened, array_file, tmp_path) -> None:
    other = tmp_path / "other.jsonl"
    other.write_text("".join(json.dumps({"k": i, "s": f"v{i}"}) + "\n" for i in range(50)), encoding="utf-8")
    s1 = opened(array_file)
    s2 = opened(other)
    panel.set_session(s1)
    ed1 = panel.current_editor()
    add_row(ed1.root, "age", "ge", "40")
    half = ed1.root.add_condition("contact.city")         # an incomplete row is part of the draft
    panel.set_session(s2)
    ed2 = panel.current_editor()
    assert ed2 is not ed1 and ed2.text() == "" and ed2.root.items() == []
    ed2.set_text("k < 10")
    assert apply_and_wait(qtbot, panel) == list(range(10))
    panel.set_session(s1)
    assert panel.current_editor() is ed1
    assert ed1.text() == "age >= 40" and ed1.root.items()[1] is half and half.field_text() == "contact.city"
    assert s1.filter is None                              # never applied to s1
    panel.set_session(s2)
    assert ed2.text() == "k < 10"
    # closing a session drops its editor; the current one -> no session
    s2.close()
    assert panel.session is None and panel.current_editor() is None
    assert panel.stack.currentWidget() is panel.notice_page
    panel.set_session(s1)
    assert panel.current_editor() is ed1


def test_follows_filters_changed_elsewhere(qtbot, panel, opened, array_file) -> None:
    s = opened(array_file)
    panel.set_session(s)
    ed = panel.current_editor()
    assert s.filter_by_value(NodeRef(0, ("contact", "city")), "Toshkent")
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert ed.text() == 'contact.city == "Toshkent"' and not ed.dirty
    assert [r.field_text() for r in ed.root.rows()] == ["contact.city"]
    # with unapplied edits the draft is kept and "load the applied filter" is offered
    ed.set_text("age > 1")
    assert ed.dirty
    s.filter_by_value(NodeRef(1, ("age",)), 1)
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert ed.text() == "age > 1"
    assert panel.load_applied_button.isVisibleTo(panel)
    panel.load_applied()
    assert ed.text() == 'contact.city == "Toshkent" and age == 1'
    assert not panel.load_applied_button.isVisibleTo(panel)
    # an applied filter that fails at run time: the session goes back, the draft stays
    ed.set_text("abs(name) > `1`", "jmespath")
    assert panel.apply()
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert s.filter.lang == "expr" and ed.text() == "abs(name) > `1`"
    assert panel.app.errors                                # reported by the session
    s.reset_view()
    qtbot.waitUntil(lambda: s.filter is None, timeout=TIMEOUT)
    assert ed.text() == "abs(name) > `1`"                 # still dirty: kept


def test_no_file_and_single_document(qtbot, panel, opened, tmp_path, lang_uz) -> None:
    assert panel.session is None and panel.stack.currentWidget() is panel.notice_page
    assert "Fayl ochilmagan" in panel.notice_page.text()
    assert not panel.apply_button.isEnabled() and not panel.apply()
    assert not panel.add_condition("a")
    path = tmp_path / "one.json"
    path.write_text('{"a": 1}', encoding="utf-8")
    s = opened(path)
    panel.set_session(s)
    assert panel.stack.currentWidget() is panel.notice_page and "bitta JSON" in panel.notice_page.text()
    assert not panel.apply_button.isEnabled() and not panel.apply() and not panel.save_preset("x")
    panel.set_session(None)
    assert "Fayl ochilmagan" in panel.notice_page.text()


def test_add_condition_keys_and_field_list(qtbot, panel, opened, tmp_path, lang_uz) -> None:
    path = tmp_path / "keyed.json"
    data = {f"user{i}": {"age": i, "city": CITIES[i % 4]} for i in range(30)}
    path.write_text(json.dumps(data), encoding="utf-8")
    s = opened(path)
    panel.show()
    panel.set_session(s)
    ed = panel.current_editor()
    qtbot.waitUntil(lambda: ed.field_names() != [], timeout=TIMEOUT)
    assert ed.field_names() == ["$key", "age", "city", "$index"]
    assert panel.add_condition("city")
    (row,) = ed.root.rows()
    assert row.field_text() == "city" and ed.text() == ""          # waits for a value
    row.v1.set_raw("Xiva")
    assert ed.text() == 'city == "Xiva"'
    assert panel.add_condition("$key", "startswith", "user1")
    assert ed.text() == 'city == "Xiva" and $key startswith "user1"'
    got = apply_and_wait(qtbot, panel)
    keys = list(data)
    assert got == [i for i, k in enumerate(keys) if data[k]["city"] == "Xiva" and k.startswith("user1")] and got
    # Enter in the expression and Ctrl+Enter anywhere apply
    ed.set_text("age < 3")
    ed.expr.setFocus()
    qtbot.keyClick(ed.expr, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 3, timeout=TIMEOUT)
    ed.set_text("age < 2")
    row = ed.root.rows()[0]
    row.v1.edit.setFocus()
    qtbot.keyClick(row.v1.edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: not s.view_busy and s.view_len() == 2, timeout=TIMEOUT)
    # JMESPath: nothing can be added
    ed.set_text("age > `1`", "jmespath")
    assert not panel.add_condition("age", "gt", 1)
    assert "qo'shib bo'lmadi" in panel.status_text()
    with pytest.raises(ValueError):
        panel.add_condition("age", "nope")


def test_status_and_larger_file(qtbot, panel, opened, tmp_path, lang_uz) -> None:
    path = tmp_path / "big.jsonl"
    n = 200000
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({"i": i, "g": i % 7, "s": f"v{i % 1000}"}) + "\n")
    s = opened(path)
    panel.set_session(s)
    assert panel.status_text() == "Filtr yo'q: 200 000 ta yozuv"
    ed = panel.current_editor()
    add_row(ed.root, "g", "in", "1, 3")
    add_row(ed.root, "s", "startswith", "v99")
    busy: list[str] = []
    s.view_busy_changed.connect(lambda b: busy.append(panel.status_text()) if b else None)
    got = apply_and_wait(qtbot, panel)
    want = [i for i in range(n) if i % 7 in (1, 3) and f"v{i % 1000}".startswith("v99")]
    assert got == want
    assert busy and busy[0].startswith("Filtrlanmoqda")
    assert panel.status_text() == f"Natija: 200 000 ta yozuvdan {widgets.format_count(len(want))} tasi"
    ed.set_text("g == 1")
    assert panel.status_text().endswith("o'zgarishlar hali qo'llanmagan")
