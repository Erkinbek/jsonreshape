"""SortDialog (jsonreshape/gui/sortdialog.py; ARCHITECTURE §11.8, AGENT_PROMPT §6.4).

Real sessions: the dialog builds exactly the SortKeys of its rows (every option), sorts the
view (checked against an independent Python sort), is prefilled from ``session.sort_keys``,
reorders / removes rows, ignores empty rows, shows path errors inline without closing,
removes the sort, works with ``open()`` and ``exec()``, offers the schema's fields
(``$key`` / ``$index``), uses the Uzbek alphabet by default, and refuses single documents.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QDialog  # noqa: E402

from jsonreshape.gui.context import AppContext  # noqa: E402
from jsonreshape.gui.guistate import GuiState  # noqa: E402
from jsonreshape.gui.session import DocSession  # noqa: E402
from jsonreshape.gui.sortdialog import SortDialog  # noqa: E402
from jsonreshape.sortkeys import SortKey  # noqa: E402

pytestmark = pytest.mark.gui

TIMEOUT = 30000


def make_records(n: int) -> list[dict]:
    names = ["sh", "ch", "o'q", "z", "a", "g'oz", "ng", "b10", "b9", "B2"]
    out = []
    for i in range(n):
        r = {"id": i, "name": names[i % len(names)], "grp": i % 4, "age": None if i % 6 == 0 else (i * 37) % 50}
        if i % 5:
            r["score"] = (i * 13) % 20
        out.append(r)
    return out


RECS = make_records(200)


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
def session(opened, tmp_path: Path) -> DocSession:
    path = tmp_path / "people.json"
    path.write_text(json.dumps(RECS), encoding="utf-8")
    return opened(path)


def make_dialog(qtbot, s: DocSession) -> SortDialog:
    dlg = SortDialog(s)
    qtbot.addWidget(dlg)
    return dlg


def fill(row, field: str, *, desc: bool = False, nulls: str = "last", natural: bool = True, collation: str = "uz",
         case: bool = False) -> None:
    row.field.setEditText(field)
    row.direction.setCurrentIndex(1 if desc else 0)
    row.nulls.setCurrentIndex(row.nulls.findData(nulls))
    row.natural.setChecked(natural)
    row.collation.setCurrentIndex(row.collation.findData(collation))
    row.case.setChecked(case)


def view(s: DocSession) -> list[int]:
    return [s.record_at(i) for i in range(s.view_len())]


def test_builds_the_sort_keys_and_sorts(qtbot, session) -> None:
    s = session
    dlg = make_dialog(qtbot, s)
    (row,) = dlg.rows()
    assert row.field_text() == "" and row.natural.isChecked()
    assert row.collation.currentData() == "uz" and not row.case.isChecked()     # defaults
    fill(row, "grp", desc=True)
    fill(dlg.add_row(), "age", nulls="first")
    fill(dlg.add_row(), "name", natural=False, collation="simple", case=True)
    assert dlg.keys() == [SortKey("grp", descending=True), SortKey("age", nulls="first"),
                          SortKey("name", natural=False, collation="simple", case_sensitive=True)]
    dlg.accept()
    assert dlg.result() == QDialog.DialogCode.Accepted
    assert s.sort_keys == dlg.keys()
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    # independent check: grp desc, then age with null/missing first, ties in file order
    want = sorted(range(len(RECS)), key=lambda i: (-RECS[i]["grp"], RECS[i]["age"] is not None,
                                                  RECS[i]["age"] or 0, i))
    got = view(s)
    assert [RECS[i]["grp"] for i in got] == [RECS[i]["grp"] for i in want]
    assert [(RECS[i]["grp"], RECS[i]["age"]) for i in got] == [(RECS[i]["grp"], RECS[i]["age"]) for i in want]


def test_prefill_reorder_remove_and_empty_rows(qtbot, session) -> None:
    s = session
    keys = [SortKey("age", descending=True, nulls="first"),
            SortKey("name", natural=False, collation="unicode", case_sensitive=True)]
    s.set_sort(keys)
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    dlg = make_dialog(qtbot, s)
    assert dlg.keys() == keys
    r1, r2 = dlg.rows()
    assert r1.direction.currentData() is True and r1.nulls.currentData() == "first"
    assert r2.collation.currentData() == "unicode" and r2.case.isChecked() and not r2.natural.isChecked()
    assert [r.number.text() for r in dlg.rows()] == ["1.", "2."]
    assert not r1.up.isEnabled() and r1.down.isEnabled() and not r2.down.isEnabled()
    dlg.move_row(r2, -1)
    assert dlg.rows() == [r2, r1] and dlg.keys() == [keys[1], keys[0]]
    assert [r.number.text() for r in dlg.rows()] == ["1.", "2."] and r2.down.isEnabled()
    r1.down.click()          # already last: nothing
    r1.up.click()
    assert dlg.rows() == [r1, r2]
    empty = dlg.add_row()
    assert dlg.keys() == keys     # rows without a field are ignored
    dlg.remove_row(r1)
    assert dlg.keys() == [keys[1]]
    r2.remove.click()
    assert dlg.rows() == [empty] and dlg.keys() == []
    dlg.remove_row(empty)
    assert len(dlg.rows()) == 1   # there is always one row
    dlg.accept()                   # no key: file order
    assert s.sort_keys == []
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert view(s) == list(range(len(RECS)))


@pytest.mark.parametrize(("field", "fragment"), [("a[", "noto'g'ri"), ("a..b", "**"), ("x.", "noto'g'ri")])
def test_invalid_paths_are_shown_inline(qtbot, session, field: str, fragment: str, lang_uz) -> None:
    s = session
    dlg = make_dialog(qtbot, s)
    good, bad = dlg.rows()[0], dlg.add_row()
    fill(good, "id")
    fill(bad, field)
    assert dlg.keys() is None
    assert bad.error_text() and fragment in bad.error_text()
    assert good.error_text() is None
    dlg.accept()
    assert dlg.result() != QDialog.DialogCode.Accepted and s.sort_keys == []
    bad.field.setEditText("age")          # editing clears the message
    assert bad.error_text() is None
    dlg.accept()
    assert s.sort_keys == [SortKey("id"), SortKey("age")]


def test_clear_sort(qtbot, session) -> None:
    s = session
    s.set_sort([SortKey("age")])
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert view(s) != list(range(len(RECS)))
    dlg = make_dialog(qtbot, s)
    dlg.clear_button.click()
    assert dlg.result() == QDialog.DialogCode.Accepted and s.sort_keys == []
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert view(s) == list(range(len(RECS)))


def test_open_and_exec(qtbot, session) -> None:
    s = session
    dlg = make_dialog(qtbot, s)
    dlg.open()
    assert dlg.isVisible()
    fill(dlg.rows()[0], "score", desc=True)
    dlg.ok_button.click()
    assert not dlg.isVisible() and s.sort_keys == [SortKey("score", descending=True)]
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    dlg2 = make_dialog(qtbot, s)
    assert dlg2.keys() == [SortKey("score", descending=True)]

    def finish() -> None:
        fill(dlg2.rows()[0], "id", desc=True)
        dlg2.accept()

    QTimer.singleShot(0, finish)
    assert dlg2.exec() == QDialog.DialogCode.Accepted
    assert s.sort_keys == [SortKey("id", descending=True)]
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert view(s) == list(range(len(RECS)))[::-1]
    dlg3 = make_dialog(qtbot, s)
    QTimer.singleShot(0, dlg3.reject)
    assert dlg3.exec() == QDialog.DialogCode.Rejected
    assert s.sort_keys == [SortKey("id", descending=True)]


def test_uzbek_alphabet_by_default(qtbot, opened, tmp_path) -> None:
    path = tmp_path / "words.jsonl"
    words = ["sh", "ch", "o'q", "z", "a", "g'oz", "ng", "b10", "b9", "B2", "x", "e"]
    path.write_text("".join(json.dumps({"w": w}) + "\n" for w in words), encoding="utf-8")
    s = opened(path)
    dlg = make_dialog(qtbot, s)
    fill(dlg.rows()[0], "w")
    dlg.accept()
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    got = [words[i] for i in view(s)]
    # a b(2 < 9 < 10, natural, case-insensitive first) e x z oʻ gʻ sh ch ng
    assert got == ["a", "B2", "b9", "b10", "e", "x", "z", "o'q", "g'oz", "sh", "ch", "ng"]


def test_field_list_keyed_and_single_document(qtbot, opened, tmp_path, lang_uz) -> None:
    path = tmp_path / "keyed.json"
    path.write_text(json.dumps({f"k{i}": {"v": i, "o": {"x": 1}} for i in range(9)}), encoding="utf-8")
    s = opened(path)
    dlg = make_dialog(qtbot, s)
    dlg.show()                                  # the fields are asked for when the dialog is shown
    qtbot.waitUntil(lambda: dlg.field_names() != [], timeout=TIMEOUT)
    assert dlg.field_names() == ["$key", "v", "o", "o.x", "$index"]
    fill(dlg.rows()[0], "$key", desc=True)
    dlg.accept()
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert view(s) == list(range(9))[::-1]
    one = tmp_path / "one.json"
    one.write_text('{"a": [1, 2]}', encoding="utf-8")
    s1 = opened(one)
    dlg1 = make_dialog(qtbot, s1)
    assert dlg1.notice.isVisibleTo(dlg1) and "bitta JSON" in dlg1.notice.text()
    assert not dlg1.ok_button.isEnabled() and not dlg1.clear_button.isEnabled()
    dlg1.accept()
    assert dlg1.result() != QDialog.DialogCode.Accepted and s1.sort_keys == []
