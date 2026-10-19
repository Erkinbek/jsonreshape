"""The recovery bars of an interrupted save (stage-3 fix round: review3_safety S2, S5, S8; review3_gui L4).

* S5 -- a roll that leaves its journal (a conflict, or an error) does not loop on "Saqlashni yakunlash":
  the bar says the save could neither be finished nor undone, names the files and offers the changes'
  JSON Patch export and deletion (the controller asks) besides trying again; a roll refused at once
  (busy) changes nothing. With a controller whose ``roll_pending`` reports the RollResult, its message is
  shown.
* L4 -- a tab that cannot edit because another tab edits the file shows one bar (the read-only one), and a
  layer held elsewhere is shown without a step count (``index.json`` counts steps only at close).
* S2 -- a file missing because the two-rename fallback of a save stopped between its renames (a real crash
  of a subprocess, backup on and off): the tab shows the journal bar besides the open error; "Saqlashni
  yakunlash" rolls the save forward in a task and opens the file; a conflict offers export / delete.
* S8 -- with the real edit controller, leftovers of interrupted saves next to an opened file are removed
  once the controller holds the file (status bar message), never while an interrupted save waits.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer

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
    write_json,
)
from jsonreshape import saver
from jsonreshape.gui import widgets
from jsonreshape.gui.doctab import DocumentTab
from jsonreshape.gui.session import DocSession
from jsonreshape.i18n import t

pytestmark = pytest.mark.gui

ROOT = Path(__file__).resolve().parents[1]
FINISH = "gui.doctab.recovery.journal.finish"
PATCH = "gui.doctab.recovery.save_patch"
DELETE = "gui.doctab.recovery.delete"


@pytest.fixture
def app(qapp, tmp_path, lang_uz):
    ctx = make_app(tmp_path)
    ctx.statuses = []
    ctx.status_message.connect(lambda text, _ms: ctx.statuses.append(text))
    yield ctx
    for w in list(widgets._kept_alive):
        w.close()
    ctx.tasks.cancel_all()
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def tabs(qtbot, app):
    made: list[DocumentTab] = []

    def factory(path, **kw) -> DocumentTab:
        kw.setdefault("use_cache", False)
        session = DocSession(app, path, **kw)
        tab = DocumentTab(session, view_registry=stub_registry())
        qtbot.addWidget(tab)
        made.append(tab)
        session.start()
        wait_ready(qtbot, session)
        return tab

    yield factory
    for tab in made:
        tab.shutdown()
    assert app.tasks.wait_idle(30)


@pytest.fixture
def stub_tabs(qtbot, tabs, monkeypatch):
    StubEditController.instances.clear()
    install_stub_edit(monkeypatch)

    def factory(path, **kw) -> DocumentTab:
        tab = tabs(path, **kw)
        qtbot.waitUntil(lambda: tab.edit is not None or tab.session.state == "failed", timeout=TIMEOUT)
        return tab

    return factory


def texts(bar) -> list[str]:
    return [b.text() for b in bar.buttons()]


def click(bar, key: str) -> None:
    for button in bar.buttons():
        if button.text() == t(key):
            button.click()
            return
    raise AssertionError(f"no button {t(key)!r} in {texts(bar)}")


def journal_info(tmp_path: Path, **kw):
    journal = {"op": "save", "v": 2, "temp": str(tmp_path / ".a.json.0123abcd.jrsave"),
               "backup": str(tmp_path / "a.json.bak"), "old_backup": str(tmp_path / ".a.json.89abcdef.old")}
    return pending_info(tmp_path, key="j" * 32, saving=journal, **kw)


# ----------------------------------------------------------------------------------------
# S5: a roll that leaves its journal
# ----------------------------------------------------------------------------------------
def test_s5_conflict_offers_export_and_delete(qtbot, stub_tabs, tmp_path) -> None:
    tab = stub_tabs(write_json(tmp_path / "a.json", records(5)))
    stub = tab.edit
    info = journal_info(tmp_path)
    stub.set(available=False, reason="recovery_pending")
    stub.find([info])
    bar = tab.info_bar("recovery")
    assert texts(bar) == [t(FINISH)] and bar.kind() == "warning"
    click(bar, FINISH)
    assert stub.calls[-1] == ("roll_pending", info)
    stub.find([info])                            # the controller: the journal is still there
    bar = tab.info_bar("recovery")
    assert bar.kind() == "error" and texts(bar) == [t(FINISH), t(PATCH), t(DELETE)]
    assert bar.message().startswith(t("gui.doctab.recovery.journal_conflict", time="")[:20])
    assert info.saving["temp"] in bar.detail() and info.saving["old_backup"] in bar.detail()
    assert not bar.close_button.isVisibleTo(bar)                  # this file version's layer: must be answered
    click(bar, DELETE)
    assert stub.calls[-1] == ("discard_pending", info, tab)
    tab.ask_save_path = lambda *a: str(tmp_path / "rescue.patch.json")
    click(bar, PATCH)
    assert stub.calls[-1] == ("export_pending", info, str(tmp_path / "rescue.patch.json"))
    click(bar, FINISH)                                             # try again (the user fixed the files)
    assert stub.calls[-1] == ("roll_pending", info)
    stub.find([pending_info(tmp_path, key="j" * 32)])              # rolled back: the layer without journal
    bar = tab.info_bar("recovery")
    assert texts(bar)[0] == t("gui.doctab.recovery.restore")
    stub.find([])
    assert tab.info_bar("recovery") is None


class RollingStub(StubEditController):
    """``roll_pending(info, *, on_done)`` like the real controller: refused at once (``refuse``) or the
    result later, after the pending layers were sent again."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.refuse = False
        self.result = None

    def roll_pending(self, info, *, on_done=None) -> None:
        self.calls.append(("roll_pending", info))
        if self.refuse:
            on_done(None)
            return

        def later() -> None:
            self.pending_found.emit(list(self.pending))
            on_done(self.result)

        QTimer.singleShot(0, later)


def test_s5_conflict_text_of_the_roll_and_refusals(qtbot, stub_tabs, tmp_path) -> None:
    tab = stub_tabs(write_json(tmp_path / "a.json", records(5)))
    stub = attach_edit(tab.session, RollingStub(tab.session))
    info = journal_info(tmp_path)
    stub.set(available=False, reason="recovery_pending")
    stub.find([info])
    stub.refuse = True                            # busy: nothing ran -- the bar stays as it was
    click(tab.info_bar("recovery"), FINISH)
    qtbot.wait(50)
    assert texts(tab.info_bar("recovery")) == [t(FINISH)]
    stub.refuse = False
    stub.result = saver.RollResult("Fayllar aniq emas: X, Y", "conflict")
    click(tab.info_bar("recovery"), FINISH)
    qtbot.waitUntil(lambda: tab.info_bar("recovery").kind() == "error", timeout=TIMEOUT)
    bar = tab.info_bar("recovery")
    assert "Fayllar aniq emas: X, Y" in bar.detail() and texts(bar) == [t(FINISH), t(PATCH), t(DELETE)]
    # an error (no RollResult, the journal still there): the actions as well
    tab2 = stub_tabs(write_json(tmp_path / "b.json", records(5)))
    stub2 = attach_edit(tab2.session, RollingStub(tab2.session))
    stub2.find([info])
    click(tab2.info_bar("recovery"), FINISH)
    qtbot.waitUntil(lambda: tab2.info_bar("recovery").kind() == "error", timeout=TIMEOUT)


# ----------------------------------------------------------------------------------------
# L4: one bar for a file edited in another tab; no unknown step count
# ----------------------------------------------------------------------------------------
def test_l4_one_bar_and_no_step_count(qtbot, stub_tabs, tmp_path) -> None:
    tab = stub_tabs(write_json(tmp_path / "a.json", records(5)))
    stub = tab.edit
    locked = pending_info(tmp_path, key="l" * 32, locked=True, steps=0, created=0.0, updated=0.0)
    stub.set(available=False, reason="locked")
    stub.find([locked])
    shown = [k for k in tab.info_bar_keys() if k in ("recovery", "readonly")]
    assert shown == ["readonly"], shown
    assert tab.info_bar("readonly").detail() == t("gui.actions.edit.reason.locked")
    # the other tab let go: this tab edits; a layer another program holds is information without a count
    stub.set(available=True, reason="")
    bar = tab.info_bar("recovery")
    assert bar is not None and tab.info_bar("readonly") is None
    assert bar.message() == t("gui.doctab.recovery.locked", time="?")
    assert "0" not in bar.message()
    stub.set(available=False, reason="locked")          # held again: back to one bar
    assert [k for k in tab.info_bar_keys() if k in ("recovery", "readonly")] == ["readonly"]


# ----------------------------------------------------------------------------------------
# S2: the file is missing (a save's fallback stopped between its two renames)
# ----------------------------------------------------------------------------------------
CHILD = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, {root!r})
    from jsonreshape import fileio
    from jsonreshape.reader import Document
    from jsonreshape.edits import EditSession, Ref
    src, backup = sys.argv[1], sys.argv[2] == "1"
    doc = Document.open(src); doc.build_index()
    es = EditSession.open(doc)
    es.set_value(Ref(5, ("name",)), '"closed for good"')
    def unsupported(a, b):
        raise fileio.ReplaceUnsupported(None, "x", str(b), 50)
    def die(*a, **k):
        os._exit(17)
    fileio.replace_posix = unsupported      # e.g. an exFAT USB stick
    fileio.hard_link = lambda a, b: False
    fileio.replace_file = die               # power cut between the two renames
    es.save(backup=backup)
""")


def crashed_between_renames(tmp_path: Path, backup: bool) -> Path:
    path = write_json(tmp_path / "data.json", records(40))
    script = tmp_path / "child.py"
    script.write_text(CHILD.format(root=str(ROOT)), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script), str(path), "1" if backup else "0"], env=dict(os.environ),
                          capture_output=True, timeout=120)
    assert proc.returncode == 17, proc.stderr.decode(errors="replace")
    assert not path.exists()
    return path


@pytest.mark.parametrize("backup", [True, False])
def test_s2_missing_file_offers_to_finish_the_save(qtbot, app, stub_tabs, tmp_path, backup) -> None:
    path = crashed_between_renames(tmp_path, backup)
    tab = stub_tabs(path)
    assert tab.session.state == "failed" and tab.info_bar("open_failed") is not None
    qtbot.waitUntil(lambda: tab.info_bar("recovery") is not None, timeout=TIMEOUT)
    bar = tab.info_bar("recovery")
    assert texts(bar) == [t(FINISH)] and tab.pending() and tab.pending()[0].saving
    assert bar.message().startswith(t("gui.doctab.recovery.journal_missing", time="")[:30])
    click(bar, FINISH)
    qtbot.waitUntil(lambda: tab.session.state == "ready" and tab.edit is not None, timeout=TIMEOUT)
    assert json.loads(path.read_text(encoding="utf-8"))[5]["name"] == "closed for good"
    assert tab.info_bar("recovery") is None and tab.info_bar("open_failed") is None
    assert any(str(path) in s for s in app.statuses)                  # the roll was told
    assert (tmp_path / "data.json.bak").exists() == backup
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".data.json")) == []
    assert saver.find_journals(path) == []


def test_s2_conflict_of_a_missing_file_offers_export_and_delete(qtbot, app, stub_tabs, tmp_path, monkeypatch) -> None:
    path = crashed_between_renames(tmp_path, True)
    info = saver.find_journals(path)[0]
    Path(info.saving["temp"]).write_bytes(b"[")                       # neither the new version ...
    Path(info.saving["backup"]).write_bytes(b"[]")                   # ... nor the original is left
    tab = stub_tabs(path)
    qtbot.waitUntil(lambda: tab.info_bar("recovery") is not None, timeout=TIMEOUT)
    click(tab.info_bar("recovery"), FINISH)
    qtbot.waitUntil(lambda: tab.info_bar("recovery") is not None and tab.info_bar("recovery").kind() == "error",
                    timeout=TIMEOUT)
    bar = tab.info_bar("recovery")
    assert texts(bar) == [t(FINISH), t(PATCH), t(DELETE)] and info.saving["temp"] in bar.detail()
    target = tmp_path / "rescue.patch.json"
    tab.ask_save_path = lambda *a: str(target)
    click(bar, PATCH)
    qtbot.waitUntil(target.exists, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: any(str(target) in s for s in app.statuses), timeout=TIMEOUT)
    ops = json.loads(target.read_text(encoding="utf-8"))
    assert any(op.get("value") == "closed for good" or "closed for good" in json.dumps(op) for op in ops)
    monkeypatch.setattr(widgets, "confirm_handler", lambda *_a: True)
    click(tab.info_bar("recovery"), DELETE)
    qtbot.waitUntil(lambda: tab.info_bar("recovery") is None, timeout=TIMEOUT)
    assert saver.find_journals(path) == [] and tab.info_bar("open_failed") is not None


# ----------------------------------------------------------------------------------------
# S8: leftovers removed after the real controller opened the file
# ----------------------------------------------------------------------------------------
def aged_leftover(path: Path, hexpart: str = "0123abcd") -> Path:
    temp = path.with_name(f".{path.name}.{hexpart}{saver.TEMP_SUFFIX}")
    temp.write_bytes(b"x" * 4096)
    past = time.time() - 3600
    os.utime(temp, (past, past))
    return temp


def test_s8_leftovers_are_removed_after_the_open(qtbot, app, tabs, tmp_path) -> None:
    path = write_json(tmp_path / "data.json", records(10))
    temp = aged_leftover(path)
    tab = tabs(path)
    qtbot.waitUntil(lambda: tab.edit is not None and tab.edit.available, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: not temp.exists(), timeout=TIMEOUT)
    message = saver.SweepResult([temp], 4096).message()
    qtbot.waitUntil(lambda: message in app.statuses, timeout=TIMEOUT)


def test_s8_no_sweep_while_an_interrupted_save_waits(qtbot, app, tabs, tmp_path) -> None:
    from jsonreshape import fileio
    from jsonreshape.edits import EditSession, Ref
    from jsonreshape.reader import Document

    path = write_json(tmp_path / "data.json", records(10))
    temp = aged_leftover(path, "89abcdef")             # not named by the journal below, but a save waits
    doc = Document.open(path)
    doc.build_index()
    es = EditSession.open(doc)
    es.set_value(Ref(1, ("name",)), '"jurnal"')
    own_temp = path.with_name(f".{path.name}.0123abcd{saver.TEMP_SUFFIX}")
    own_temp.write_bytes(b'[{"id": "saved"}]')
    old, new = fileio.file_identity(path), fileio.file_identity(own_temp)
    es.store.journal({"v": 1, "op": "save", "source": str(path), "temp": str(own_temp), "backup": None,
                      "had_backup": False, "old_backup": None,
                      "aside": str(path.with_name(f".{path.name}.22222222.old")),
                      "old_identity": old.to_dict(), "new": {"size": new.size, "hashes": list(new.hashes)},
                      "time": time.time()})
    es.close()
    doc.close()
    tab = tabs(path)
    qtbot.waitUntil(lambda: tab.edit is not None and tab.edit.reason == "recovery_pending", timeout=TIMEOUT)
    qtbot.waitUntil(lambda: tab.info_bar("recovery") is not None, timeout=TIMEOUT)
    assert app.tasks.wait_idle(30)
    assert temp.exists() and own_temp.exists()
    click(tab.info_bar("recovery"), FINISH)                           # rolled back: the journal is gone
    qtbot.waitUntil(lambda: not own_temp.exists(), timeout=TIMEOUT)
    qtbot.waitUntil(lambda: not temp.exists(), timeout=TIMEOUT)          # now the leftover goes
    assert len(tab.edit.pending) == 1 and not tab.edit.pending[0].saving
