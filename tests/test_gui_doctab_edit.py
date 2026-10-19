"""Editing in the document tab and the status bar (ARCHITECTURE §12.9): the recovery bar
(``pending_found``: restore / discard / save as JSON Patch; locked = information only;
stale / damaged = export / delete; an interrupted save journal first), the read-only note,
the "view outdated -- Refresh" bar, the file-changed bar offering the changes as a JSON
Patch (``diff=False``), every reopen entry point asking first (F5 / the bars' buttons / the
record path dialog), the ``*`` title, the status bar's "● N o'zgarish" indicator.

Controller = ``guishell_helpers.StubEditController`` (§12.9 surface), views = stubs."""

from __future__ import annotations

import time

import pytest

from guishell_helpers import (
    TIMEOUT,
    StubEditController,
    attach_edit,
    install_stub_edit,
    make_app,
    pending_info,
    records,
    stub_registry,
    wait_ready,
    wrapped_file,
    write_json,
)
from jsonreshape.gui import widgets
from jsonreshape.gui.doctab import BAR_ORDER, DIRTY_MARK, DocumentTab, confirm_drop_edits, reopen_session
from jsonreshape.gui.navigation import BookmarksDialog, RecordPathDialog
from jsonreshape.gui.session import DocSession, NodeRef
from jsonreshape.gui.statusbar import StatusBar
from jsonreshape.i18n import t

pytestmark = pytest.mark.gui


@pytest.fixture
def app(qapp, tmp_path, lang_uz):
    ctx = make_app(tmp_path)
    yield ctx
    for w in list(widgets._kept_alive):
        w.close()
    ctx.tasks.cancel_all()
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def stubs(monkeypatch):
    StubEditController.instances.clear()
    return install_stub_edit(monkeypatch)


@pytest.fixture
def make_tab(qtbot, app, stubs):
    tabs: list[DocumentTab] = []

    def factory(path, **kw) -> DocumentTab:
        kw.setdefault("use_cache", False)
        session = DocSession(app, path, **kw)
        tab = DocumentTab(session, view_registry=stub_registry())
        qtbot.addWidget(tab)
        tabs.append(tab)
        session.start()
        wait_ready(qtbot, session)
        qtbot.waitUntil(lambda: tab.edit is not None or session.state == "failed", timeout=TIMEOUT)
        return tab

    yield factory
    for tab in tabs:
        tab.shutdown()
    assert app.tasks.wait_idle(30)


def bar_texts(bar) -> list[str]:
    return [b.text() for b in bar.buttons()]


def click(bar, text: str) -> None:
    for button in bar.buttons():
        if button.text() == text:
            button.click()
            return
    raise AssertionError(f"no button {text!r} in {bar_texts(bar)}")


def track_reopen(session) -> list[dict]:
    calls: list[dict] = []
    real = session.reopen
    session.reopen = lambda **kw: (calls.append(kw), real(**kw))
    return calls


# ----------------------------------------------------------------------------------------
# helpers of the module
# ----------------------------------------------------------------------------------------
def test_confirm_drop_edits_and_reopen_session(qtbot, app, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(5)))
    answers = []
    confirm_drop_edits(tab.session, tab, answers.append, why="reopen")
    assert answers == [True] and "ask_close" not in tab.edit.names()        # clean: nothing asked
    tab.edit.set(dirty=True, steps=1)
    tab.edit.answer = "cancel"
    confirm_drop_edits(tab.session, tab, answers.append, why="reopen")
    assert answers == [True, False] and tab.edit.calls[-2] == ("ask_close", "reopen")

    class Broken(StubEditController):
        def ask_close(self, parent, on_done, *, why):
            raise RuntimeError("boom")

    broken = Broken(dirty=True)
    tab.session.edit = broken
    confirm_drop_edits(tab.session, tab, answers.append)
    assert answers[-1] is False and isinstance(app.errors[-1], RuntimeError)      # reported, not raised
    app.errors.clear()
    calls = track_reopen(tab.session)
    tab.session.edit = None
    reopen_session(tab.session, format="array")
    assert calls == [{"discard_edits": True, "format": "array"}]
    wait_ready(qtbot, tab.session)


def test_bar_order_has_the_edit_bars() -> None:
    for key in ("recovery", "view_outdated", "readonly"):
        assert key in BAR_ORDER
    assert BAR_ORDER.index("recovery") < BAR_ORDER.index("file_changed") < BAR_ORDER.index("view_outdated")


# ----------------------------------------------------------------------------------------
# recovery bar
# ----------------------------------------------------------------------------------------
def test_recovery_bar_matching_layer(qtbot, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(5)))
    stub = tab.edit
    info = pending_info(tmp_path, steps=7, label="Kalit qayta nomlandi")
    stub.set(available=False, reason="recovery_pending")
    stub.find([info])
    bar = tab.info_bar("recovery")
    assert bar is not None and bar.kind() == "warning"
    assert "7" in bar.message() and "Kalit qayta nomlandi" in bar.detail()
    assert bar_texts(bar) == [t("gui.doctab.recovery.restore"), t("gui.doctab.recovery.discard"),
                              t("gui.doctab.recovery.save_patch")]
    assert not bar.close_button.isVisibleTo(bar)               # must be answered
    assert tab.info_bar("readonly") is None                    # the recovery bar says it
    click(bar, t("gui.doctab.recovery.restore"))
    assert stub.calls[-1] == ("restore_pending", info)
    click(bar, t("gui.doctab.recovery.discard"))
    assert stub.calls[-1] == ("discard_pending", info, tab)
    tab.ask_save_path = lambda title, default, filters: str(tmp_path / "saved.patch.json")
    click(bar, t("gui.doctab.recovery.save_patch"))
    assert stub.calls[-1] == ("export_pending", info, str(tmp_path / "saved.patch.json"))
    tab.ask_save_path = lambda *a: None                        # cancelled: nothing
    n = len(stub.calls)
    click(bar, t("gui.doctab.recovery.save_patch"))
    assert len(stub.calls) == n
    # restored: the controller reports what is left (nothing), the bar goes away
    stub.set(available=True, reason="", dirty=True, steps=7)
    assert tab.info_bar("recovery") is not None               # a state change alone does not hide it
    stub.find([])
    assert tab.info_bar("recovery") is None
    assert tab.title() == f"{DIRTY_MARK}a.json"


def test_recovery_bar_kinds_and_order(qtbot, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(5)))
    stub = tab.edit
    locked = pending_info(tmp_path, key="l" * 32, locked=True)
    stale = pending_info(tmp_path, key="s" * 32, matches=False, steps=2)
    damaged = pending_info(tmp_path, key="d" * 32, damaged="seg-00000.dat yo'q")
    journal = pending_info(tmp_path, key="j" * 32, saving={"op": "save"})
    stub.find([locked, stale, damaged, journal])
    bar = tab.info_bar("recovery")
    # an interrupted save comes first; it must be answered
    assert bar_texts(bar) == [t("gui.doctab.recovery.journal.finish")]
    assert t("gui.doctab.recovery.more", count=3) in bar.detail()
    assert not bar.close_button.isVisibleTo(bar)
    click(bar, t("gui.doctab.recovery.journal.finish"))
    assert stub.calls[-1] == ("roll_pending", journal)
    stub.find([locked, stale, damaged])                         # the controller reports what is left
    bar = tab.info_bar("recovery")
    assert bar.kind() == "error" and "seg-00000.dat yo'q" in bar.detail()
    assert bar_texts(bar) == [t("gui.doctab.recovery.save_patch"), t("gui.doctab.recovery.delete")]
    assert not bar.close_button.isVisibleTo(bar)               # a damaged layer of this version blocks
    click(bar, t("gui.doctab.recovery.delete"))
    assert stub.calls[-1] == ("discard_pending", damaged, tab)
    stub.find([locked, stale])
    bar = tab.info_bar("recovery")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(stale.updated))
    assert bar.message() == t("gui.doctab.recovery.stale", time=when, steps="2")
    assert bar.detail().startswith(t("gui.doctab.recovery.stale.detail"))
    assert bar_texts(bar) == [t("gui.doctab.recovery.save_patch"), t("gui.doctab.recovery.delete")]
    assert bar.close_button.isVisibleTo(bar)                    # an older version's layer may be put aside
    bar.close_bar()
    assert stub.calls[-1] == ("ignore_pending", stale)
    bar = tab.info_bar("recovery")                              # what is left: the locked one
    assert bar.kind() == "info" and bar_texts(bar) == [] and bar.close_button.isVisibleTo(bar)
    assert bar.message().startswith(t("gui.doctab.recovery.locked", time="", steps="")[:20])
    bar.close_bar()                                             # information only: closable, stays closed
    assert stub.calls[-1] == ("ignore_pending", locked)
    assert tab.info_bar("recovery") is None
    stub.pending_found.emit([locked])                           # the same info again: stays closed
    assert tab.info_bar("recovery") is None
    stub.find([])
    assert tab.info_bar("recovery") is None


def test_recovery_bar_without_roll_journal_uses_restore(qtbot, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(5)))

    class NoRoll(StubEditController):
        roll_pending = None    # type: ignore[assignment]

    stub = attach_edit(tab.session, NoRoll(tab.session))
    journal = pending_info(tmp_path, saving={"op": "save"})
    stub.pending_found.emit([journal])
    click(tab.info_bar("recovery"), t("gui.doctab.recovery.journal.finish"))
    assert stub.calls[-1] == ("restore_pending", journal)


def test_recovery_bar_follows_controller_replacement(qtbot, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(5)))
    old = tab.edit
    old.pending_found.emit([pending_info(tmp_path)])
    assert tab.info_bar("recovery") is not None
    tab.reopen()                                               # clean: no question; a new controller later
    assert tab.info_bar("recovery") is None
    wait_ready(qtbot, tab.session)
    qtbot.waitUntil(lambda: tab.edit is not None and tab.edit is not old, timeout=TIMEOUT)
    old.pending_found.emit([pending_info(tmp_path)])           # the detached one is not listened to
    assert tab.info_bar("recovery") is None
    known = pending_info(tmp_path, steps=9)
    attach_edit(tab.session, StubEditController(tab.session, pending=[known]))   # found layers already
    assert tab.pending() == [known] and tab.info_bar("recovery") is not None


# ----------------------------------------------------------------------------------------
# read-only note, view outdated, file changed
# ----------------------------------------------------------------------------------------
def test_readonly_note(qtbot, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(5)))
    stub = tab.edit
    assert tab.info_bar("readonly") is None
    stub.set(available=False, reason="locked")
    bar = tab.info_bar("readonly")
    assert bar is not None and bar.message() == t("gui.doctab.readonly")
    assert bar.detail() == t("gui.actions.edit.reason.locked")
    for quiet in ("indexing", "saving", "applying", "recovery_pending", "file_changed", "closed"):
        stub.set(reason=quiet)
        assert tab.info_bar("readonly") is None, quiet
    stub.set(reason="huge_single")
    assert tab.info_bar("readonly").detail() == t("gui.actions.edit.reason.huge_single")
    tab.info_bar("readonly").close_bar()
    stub.set(reason="huge_single")
    assert tab.info_bar("readonly") is None                    # closed: stays closed for that reason
    stub.set(reason="unavailable")
    assert tab.info_bar("readonly") is not None
    stub.set(available=True, reason="")
    assert tab.info_bar("readonly") is None


def test_grown_bar_says_why_editing_is_off(qtbot, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(5)))
    stub = tab.edit
    tab.session.file_changed.emit("grown")
    bar = tab.info_bar("file_changed")
    assert bar is not None and bar.kind() == "info" and bar.detail() is None
    stub.set(available=False, reason="file_grown")              # the controller follows the growth
    assert tab.info_bar("readonly") is None                    # one bar says it
    assert tab.info_bar("file_changed").detail() == t("gui.actions.edit.reason.file_grown")
    stub.set(available=True, reason="")
    assert tab.info_bar("file_changed").detail() is None


def test_view_outdated_bar(qtbot, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(20)))
    session = tab.session
    if not hasattr(session, "_set_outdated"):
        pytest.skip("the session has no outdated-view state")
    session._set_outdated(True)
    bar = tab.info_bar("view_outdated")
    assert bar is not None and bar_texts(bar) == [t("gui.doctab.view_outdated.refresh")]
    assert not bar.close_button.isVisibleTo(bar)
    click(bar, t("gui.doctab.view_outdated.refresh"))
    assert not session.view_outdated
    assert tab.info_bar("view_outdated") is None


def test_file_changed_bar_offers_the_patch_when_dirty(qtbot, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(5)))
    stub = tab.edit
    tab.session.file_changed.emit("changed")
    bar = tab.info_bar("file_changed")
    assert bar_texts(bar) == [t("gui.doctab.reopen"), t("gui.doctab.ignore")]
    stub.set(dirty=True, steps=2)                              # the bar follows the dirty state
    bar = tab.info_bar("file_changed")
    assert bar_texts(bar) == [t("gui.doctab.reopen"), t("gui.doctab.changed.save_patch"), t("gui.doctab.ignore")]
    assert bar.detail() == t("gui.doctab.changed.detail_dirty")
    tab.ask_save_path = lambda title, default, filters: str(tmp_path / "changes.json")
    click(bar, t("gui.doctab.changed.save_patch"))
    assert stub.calls[-1] == ("export_patch", str(tmp_path / "changes.json"), False)      # diff=False
    # Reopen asks first (the controller adapts its buttons to the changed file)
    stub.answer = "cancel"
    calls = track_reopen(tab.session)
    click(tab.info_bar("file_changed"), t("gui.doctab.reopen"))
    assert calls == [] and ("ask_close", "reopen") in stub.calls
    stub.answer = "discard"
    click(tab.info_bar("file_changed"), t("gui.doctab.reopen"))
    assert calls == [{"discard_edits": True}]
    wait_ready(qtbot, tab.session)


def test_reopen_entry_points_ask_first(qtbot, make_tab, tmp_path) -> None:
    path = wrapped_file(tmp_path / "w.json", 20)
    tab = make_tab(path)
    stub = tab.edit
    stub.set(dirty=True, steps=1)
    stub.answer = "cancel"
    calls = track_reopen(tab.session)
    tab.reopen()
    tab.reopen(format="jsonl")
    # the record path dialog: a Cancel keeps it open
    dialog = RecordPathDialog(tab.session, tab)
    qtbot.addWidget(dialog)
    dialog.open()
    dialog.path_edit.setText("other")
    dialog.accept()
    assert calls == [] and dialog.isVisible()
    assert [c for c in stub.calls if c[0] == "ask_close"] == [("ask_close", "reopen")] * 3
    # an answer later: the dialog closes then, the session reopens with the choice
    stub.answer = None
    dialog.accept()
    dialog.accept()                                            # asked once only
    assert len(stub.questions) == 1 and dialog.isVisible()
    stub.answer_question("discard")
    assert calls == [{"discard_edits": True, "record_path": "other", "format": None}]
    assert not dialog.isVisible()
    wait_ready(qtbot, tab.session)
    assert tab.session.doc.record_count == 3


def test_open_failure_buttons_go_through_reopen(qtbot, make_tab, tmp_path) -> None:
    path = tmp_path / "plain.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    tab = make_tab(path, format="keyed")
    assert tab.session.state == "failed"
    bar = tab.info_bar("open_failed")
    calls = track_reopen(tab.session)
    click(bar, t("gui.doctab.open_auto"))
    assert calls == [{"discard_edits": True, "record_path": None, "format": None}]
    wait_ready(qtbot, tab.session)
    assert tab.session.state == "ready"


# ----------------------------------------------------------------------------------------
# status bar
# ----------------------------------------------------------------------------------------
def test_status_bar_dirty_indicator(qtbot, app, make_tab, tmp_path) -> None:
    bar = StatusBar(app)
    qtbot.addWidget(bar)
    tab = make_tab(write_json(tmp_path / "a.json", records(5)))
    bar.set_tab(tab)
    assert bar.dirty_label.isHidden() and bar.dirty_text() == ""
    tab.edit.set(dirty=True, steps=3, undo_text="Qiymat o'zgartirildi")
    assert bar.dirty_text() == "● 3 o'zgarish" and not bar.dirty_label.isHidden()
    assert bar.dirty_label.full_text() == "● 3 o'zgarish"
    assert "Qiymat o'zgartirildi" in bar.dirty_label.tip()
    labels = bar.labels()
    assert labels.index(bar.dirty_label) == labels.index(bar.count_label) + 1      # next to the count
    tab.edit.set(steps=12000)
    assert bar.dirty_text() == t("gui.status.dirty", count="12 000")
    tab.edit.set(dirty=False, steps=0)
    assert bar.dirty_label.isHidden()
    bar.set_tab(None)
    assert bar.dirty_label.isHidden()
    bar.shutdown()


def test_status_bar_english_dirty_text(qtbot, app, make_tab, tmp_path, lang_en) -> None:
    bar = StatusBar(app)
    qtbot.addWidget(bar)
    tab = make_tab(write_json(tmp_path / "a.json", records(5)))
    bar.set_tab(tab)
    tab.edit.set(dirty=True, steps=1)
    assert bar.dirty_text() == "● 1 changes"
    bar.shutdown()


# ----------------------------------------------------------------------------------------
# bookmarks dialog
# ----------------------------------------------------------------------------------------
def test_bookmarks_dialog_uses_the_session_positions(qtbot, app, make_tab, tmp_path) -> None:
    tab = make_tab(write_json(tmp_path / "a.json", records(10)))
    session = tab.session
    session.add_bookmark("yettinchi", NodeRef(7, ("name",)))
    session.add_bookmark("ikkinchi", NodeRef(2, ()))
    dialog = BookmarksDialog(session)
    qtbot.addWidget(dialog)
    assert dialog.tree.topLevelItem(0).text(1) == "#7.name"
    # a bookmark whose record the session cannot place any more (deleted by an edit)
    real = session.bookmark_ref
    session.bookmark_ref = lambda i: None if i == 0 else real(i)
    dialog.refresh()
    assert dialog.tree.topLevelItem(0).text(1) == t("gui.nav.bookmarks.deleted")
    assert dialog.tree.topLevelItem(0).toolTip(1) == t("gui.nav.bookmarks.deleted.tip")
    assert dialog.tree.topLevelItem(1).text(1) == "#2"
    dialog.go(0)
    assert dialog.error_label.isVisibleTo(dialog) and dialog.error_label.text() == t("gui.nav.bookmarks.deleted.tip")
    # positions move with edits: the list follows data_changed
    session.bookmark_ref = lambda i: NodeRef(3, ("name",)) if i == 0 else real(i)
    if hasattr(session, "data_changed"):
        session.data_changed.emit(None)
        assert dialog.tree.topLevelItem(0).text(1) == "#3.name"
    with qtbot.waitSignal(dialog.resolved, timeout=TIMEOUT):
        dialog.go(1)
    assert session.current is not None and session.current.record == 2
    assert "Ctrl+B" in t("gui.nav.bookmarks.empty") and "Ctrl+D" not in t("gui.nav.bookmarks.empty")
