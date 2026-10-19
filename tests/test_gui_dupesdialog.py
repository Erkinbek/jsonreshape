"""DupesDialog (docs/ARCHITECTURE.md §14.9; scratchpad s5/map_stats.md §7.1) with real DocSession objects.

Key list (no ``[*]`` fields, ``$key``, ``$``) and the finder's own error for wildcard keys, keep first / last,
scope current view / all records, the counts and warnings, the 20 example groups (record activation reveals the
key node, a group's "Filtr" -- also for key texts cut at 120 characters, read again in a silent task), "show
duplicates" (``session.show_records``), the export callback and the default export dialog, "remove duplicates…"
-> the stage-3 bulk dedupe preview (BulkDialog prefilled through its public API), Stop, the stale result after an
edit / a reopen, the registry of :func:`show_dupes_dialog` and the Uzbek texts. Both core backends.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from jsonreshape import dupes
from jsonreshape.bulk import BulkSpec
from jsonreshape.gui import dupesdialog
from jsonreshape.gui.dupesdialog import DupesDialog, show_dupes_dialog
from jsonreshape.gui.session import FilterSpec, NodeRef
from jsonreshape.i18n import t
from jsonreshape.recordset import RecordSet
from stats_gui_helpers import (  # noqa: F401 - fixtures
    KIND_CASES,
    TIMEOUT,
    app,
    backend,
    count_matching,
    opened,
    reset,
    wait_view,
    write_keyed,
    write_kind,
    write_records,
)

pytestmark = pytest.mark.gui


@pytest.fixture
def dialog(qtbot):
    made: list[DupesDialog] = []

    def make(session, **kw) -> DupesDialog:
        dlg = DupesDialog(session, **kw)
        qtbot.addWidget(dlg)
        made.append(dlg)
        return dlg

    yield make
    for dlg in made:
        try:
            dlg.close()
        except RuntimeError:  # already deleted
            pass


@pytest.fixture
def records_path(tmp_path: Path) -> Path:
    return write_records(tmp_path / "records.json")


def find(qtbot, dlg: DupesDialog, key: str, *, keep: str = "first", scope: str = "all"):
    dlg.set_key(key)
    dlg.set_keep(keep)
    dlg.set_scope(scope)
    task = dlg.run()
    assert task is not None, dlg.status_text()
    qtbot.waitUntil(lambda: dlg.status() == "done", timeout=TIMEOUT)
    assert dlg.report is not None
    return dlg.report


# ----------------------------------------------------------------------------------
# inputs
# ----------------------------------------------------------------------------------
def test_key_list_and_wildcard_refusal(qtbot, opened, dialog, records_path: Path, tmp_path: Path) -> None:
    s = opened(records_path)
    dlg = dialog(s)
    qtbot.waitUntil(lambda: "city" in dlg.key_items(), timeout=TIMEOUT)
    items = dlg.key_items()
    assert {"id", "city", "score", "note", "orders", "tags", "mix", "$"} <= set(items)
    assert not any("[*]" in item for item in items) and "$key" not in items
    assert items[-1] == "$"
    dlg.set_key("orders[*].sum")
    assert dlg.key_error.isVisibleTo(dlg)
    assert dlg.key_error.text() == t("dupes.error.wildcard", key="orders[*].sum")
    assert not dlg.run_button.isEnabled() and dlg.run() is None
    dlg.set_key("")
    assert dlg.run() is None and dlg.status_text() == t("gui.dupesdialog.key.empty")
    k = opened(write_keyed(tmp_path / "keyed.json"))
    dlg2 = dialog(k)
    qtbot.waitUntil(lambda: "$key" in dlg2.key_items(), timeout=TIMEOUT)
    assert dlg2.key_items()[0] == "$key"


# ----------------------------------------------------------------------------------
# results
# ----------------------------------------------------------------------------------
def test_counts_groups_and_kept_marker(qtbot, opened, dialog, records_path: Path, backend) -> None:
    s = opened(records_path)
    dlg = dialog(s)
    got: list = []
    dlg.report_ready.connect(got.append)
    rep = find(qtbot, dlg, "city")
    assert got == [rep]
    assert (rep.groups, rep.duplicates, rep.records_with_key, rep.missing) == (4, 56, 60, 0)
    counts = {name: dlg.count_text(name) for name in dupesdialog.COUNT_ROWS}
    assert counts == {"scanned": "60", "with_key": "60", "missing": "0", "skipped": "0", "distinct": "4",
                      "groups": "4", "duplicates": "56"}
    assert dlg.group_count() == 4
    assert dlg.groups_title.text() == t("gui.dupesdialog.groups.title", count="4")
    assert dlg.group_key_text(0) == '"Toshkent"' and dlg.group_size(0) == 15
    assert dlg.group_positions(0) == list(range(0, 60, 4))
    first = dlg.groups.topLevelItem(0)
    assert first.child(0).text(0) == "#0 " + t("gui.dupesdialog.groups.kept") and first.child(1).text(0) == "#4"
    assert dlg.group_key_text(2) == '"Ўзбекистон"'
    rep = find(qtbot, dlg, "note", keep="last")
    assert (rep.groups, rep.records_with_key, rep.missing, rep.duplicates) == (5, 50, 10, 45)   # null is a value
    assert dlg.count_text("distinct") == "5" and dlg.count_text("missing") == "10"
    assert dlg.group_key_text(0) == "null"
    last = dlg.groups.topLevelItem(0)
    assert last.child(last.childCount() - 1).text(0).endswith(t("gui.dupesdialog.groups.kept"))
    rep = find(qtbot, dlg, "nosuch")
    assert rep.records_with_key == 0 and dlg.warnings_label.isVisibleTo(dlg)
    assert dlg.warnings_label.text() == "\n".join(rep.warning_texts())
    rep = find(qtbot, dlg, "id")
    assert rep.groups == 0 and dlg.groups_title.text() == t("gui.dupesdialog.groups.none")
    for button in (dlg.show_button, dlg.export_button, dlg.remove_button):
        assert not button.isEnabled() and button.toolTip() == t("gui.dupesdialog.why.none")


def test_twenty_groups_and_more_positions(qtbot, opened, dialog, tmp_path: Path) -> None:
    path = tmp_path / "groups.json"
    path.write_text(json.dumps([{"g": i % 30, "h": i % 2} for i in range(90)]), encoding="utf-8")
    s = opened(path)
    dlg = dialog(s)
    rep = find(qtbot, dlg, "g")
    assert rep.groups == 30 and dlg.group_count() == 20          # the first 20 groups by position
    assert dlg.groups_title.text() == t("gui.dupesdialog.groups.title", count="20")
    rep = find(qtbot, dlg, "h")
    assert rep.groups == 2 and dlg.group_size(0) == 45 and len(dlg.group_positions(0)) == 20
    group = dlg.groups.topLevelItem(0)
    assert group.child(group.childCount() - 1).text(0) == t("gui.dupesdialog.groups.more", count="25")


def test_scope_view_and_keep_last_show_duplicates(qtbot, opened, dialog, records_path: Path, backend) -> None:
    s = opened(records_path)
    s.set_filter(FilterSpec("id < 20"))
    wait_view(qtbot, s, 20)
    dlg = dialog(s)
    assert dlg.scope() == "view"
    assert dlg.scope_buttons["view"].text() == t("gui.dupesdialog.scope.view", count="20")
    rep = find(qtbot, dlg, "city", keep="last", scope="view")
    assert rep.scanned == 20 and rep.groups == 4 and rep.duplicates == 16
    task = dlg.show_duplicates()
    assert task is not None
    wait_view(qtbot, s, 16)
    assert s.shown_records_label == t("dupes.desc", key="city")
    shown = [s.record_at(i) for i in range(s.view_len())]
    kept = {16, 17, 18, 19}                                  # the last record of each city group among #0..#19
    assert sorted(shown) == sorted(set(range(20)) - kept)
    assert dlg.stale() is None                               # our own view change is not a stale result
    assert dlg.remove_duplicates() is None                   # the view searched is not the current one any more
    assert dlg.status_text() == t("gui.dupesdialog.remove.view_changed")


# ----------------------------------------------------------------------------------
# reveal / filter
# ----------------------------------------------------------------------------------
def test_reveal_and_group_filter(qtbot, opened, dialog, records_path: Path) -> None:
    s = opened(records_path)
    dlg = dialog(s)
    find(qtbot, dlg, "city")
    group = dlg.groups.topLevelItem(1)
    with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT) as sig:
        dlg.groups.itemActivated.emit(group.child(2), 0)
    assert sig.args[0] == NodeRef(9, ("city",)) and s.current == NodeRef(9, ("city",))
    with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT) as sig:
        dlg.groups.itemActivated.emit(group, 0)                # a group: its first record
    assert sig.args[0] == NodeRef(1, ("city",))
    dlg.groups.setCurrentItem(group)
    assert dlg.filter_button.isEnabled()
    dlg.filter_button.click()
    wait_view(qtbot, s, 15)
    assert s.filter.text == 'city == "Samarqand"'
    reset(qtbot, s)
    menu = dlg.group_menu(dlg.groups.topLevelItem(2))
    texts = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert texts == [t("gui.dupesdialog.menu.reveal"), t("gui.dupesdialog.menu.filter_key"),
                     t("gui.dupesdialog.menu.copy")]
    menu.actions()[1].trigger()
    wait_view(qtbot, s, 15)
    assert s.filter.text == 'city == "Ўзбекистон"'
    reset(qtbot, s)
    find(qtbot, dlg, "score")
    i = [dlg.group_key_text(k) for k in range(dlg.group_count())].index("12345678901234567890")
    assert dlg.filter_group(i) is True
    wait_view(qtbot, s, 12)
    assert s.filter.text == "score == 12345678901234567890"


def test_whole_record_key_and_cut_key_texts(qtbot, opened, dialog, tmp_path: Path, backend) -> None:
    path = tmp_path / "whole.json"
    long_a, long_b = "a" * 200, "O'zbek " * 30
    path.write_text("[" + ", ".join([
        '{"v": 1, "long": "%s"}' % long_a, '{"v": 1.0, "long": "%s"}' % long_b, '{"v": 2, "long": "%s"}' % long_a,
        '{"v": 1.00, "long": "%s"}' % long_b, '{"long": "%s", "v": 1}' % long_a]) + "]", encoding="utf-8")
    s = opened(path)
    dlg = dialog(s)
    rep = find(qtbot, dlg, "$")
    assert rep.groups == 2 and dlg.group_positions(0) == [0, 4]          # member order does not matter
    assert dlg.group_positions(1) == [1, 3]                              # 1.0 == 1.00
    find(qtbot, dlg, "v")
    assert dlg.group_key_text(0) == "1" and dlg.group_size(0) == 4
    with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT) as sig:
        dlg.reveal_position(3)
    assert sig.args[0] == NodeRef(3, ("v",))
    assert dlg.filter_group(0) is True
    wait_view(qtbot, s, 4)                                       # 1 == 1.0 == 1.00
    reset(qtbot, s)
    find(qtbot, dlg, "long")
    texts = [dlg.group_key_text(k) for k in range(dlg.group_count())]
    assert all(text.endswith("…") and len(text) == 120 for text in texts)
    task = dlg.filter_group(texts.index(json.dumps(long_b)[:119] + "…"))
    assert task is not None and task is not True                 # the value is read again in a silent task
    wait_view(qtbot, s, 2)
    assert s.filter.text == f"long == {json.dumps(long_b)}"
    reset(qtbot, s)
    find(qtbot, dlg, "$")
    assert dlg.filter_group(0) is not None                       # cut text: read again
    wait_view(qtbot, s, count_matching(s, '$ == {"v":1,"long":"%s"}' % long_a))
    assert s.view_len() == 2


def test_keyed_document_key(qtbot, opened, dialog, tmp_path: Path) -> None:
    s = opened(write_keyed(tmp_path / "keyed.json"))
    dlg = dialog(s)
    rep = find(qtbot, dlg, "$key")
    assert rep.groups == 0 and rep.records_with_key == 12
    rep = find(qtbot, dlg, "name")
    assert rep.groups == 4 and rep.duplicates == 8
    with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT) as sig:
        dlg.reveal_position(dlg.group_positions(0)[1])
    assert sig.args[0] == NodeRef(4, ("name",))


# ----------------------------------------------------------------------------------
# actions
# ----------------------------------------------------------------------------------
def test_export_callback_and_default_export_dialog(qtbot, opened, dialog, records_path: Path) -> None:
    s = opened(records_path)
    got: list = []
    dlg = dialog(s, open_export=lambda rs, label: got.append((rs, label)))
    rep = find(qtbot, dlg, "code")
    assert rep.groups == 7 and rep.duplicates == 53
    assert dlg.export() is not None
    qtbot.waitUntil(lambda: bool(got), timeout=TIMEOUT)
    rs, label = got[0]
    try:
        assert isinstance(rs, RecordSet) and len(rs) == 53 and label == t("dupes.desc", key="code")
        assert rs.to_list() == rep.records.to_list() and rs.gen == rep.records.gen
    finally:
        rs.close()
    dlg.open_export = None
    assert dlg.export() is not None
    qtbot.waitUntil(lambda: dlg.export_dialog is not None, timeout=TIMEOUT)
    try:
        assert dlg.export_dialog.scope() == "view"
        wait_view(qtbot, s, 53)
        assert s.shown_records_label == label
    finally:
        dlg.export_dialog.close()


def test_remove_duplicates_opens_the_bulk_dedupe_preview(qtbot, opened, dialog, records_path: Path) -> None:
    s = opened(records_path, edit=True)
    calls: list = []
    dlg = dialog(s, open_bulk=lambda spec, scope: calls.append((spec, scope)))
    find(qtbot, dlg, "city", keep="last", scope="all")
    assert dlg.remove_button.isEnabled()
    dlg.remove_button.click()
    assert len(calls) == 1 and calls[0][1] == "all"
    spec = calls[0][0]
    assert isinstance(spec, BulkSpec) and spec.kind == "dedupe" and spec.params == {"key": "city", "keep": "last"}
    dlg.open_bulk = None
    bulk = dlg.remove_duplicates()
    try:
        assert bulk is dlg.bulk_dialog and bulk.spec().params == {"key": "city", "keep": "last"}
        assert bulk.scope_choice() == "all"
        qtbot.waitUntil(lambda: bulk.preview_result is not None and bulk.task is None, timeout=TIMEOUT)
        assert bulk.preview_result.records_changed == 56 and bulk.can_apply()
    finally:
        bulk.close()
    s.set_filter(FilterSpec("id < 20"))
    wait_view(qtbot, s, 20)
    find(qtbot, dlg, "city", scope="view")
    dlg.open_bulk = lambda spec, scope: calls.append((spec, scope))
    dlg.remove_duplicates()
    assert calls[-1][1] == "view"                               # the filtered view searched is still the view


# ----------------------------------------------------------------------------------
# stop, stale, reopen, close
# ----------------------------------------------------------------------------------
@pytest.fixture
def gated(monkeypatch):
    gate = threading.Event()
    started = threading.Event()
    real = dupes.find_duplicates

    def fake(doc, key, source=None, **kw):
        started.set()
        gate.wait(20)
        return real(doc, key, source, **kw)

    monkeypatch.setattr(dupesdialog, "find_duplicates", fake)
    yield gate, started
    gate.set()


def test_stop(qtbot, opened, dialog, records_path: Path, gated) -> None:
    gate, started = gated
    s = opened(records_path)
    dlg = dialog(s)
    dlg.set_key("city")
    assert dlg.run() is not None
    qtbot.waitUntil(started.is_set, timeout=TIMEOUT)
    assert dlg.stop_button.isVisibleTo(dlg) and not dlg.run_button.isVisibleTo(dlg)
    assert not dlg.key_combo.isEnabled()
    dlg.stop()
    gate.set()
    qtbot.waitUntil(lambda: dlg.status() == "cancelled", timeout=TIMEOUT)
    assert dlg.report is None and dlg.status_text() == t("gui.dupesdialog.status.cancelled")
    assert dlg.run_button.isVisibleTo(dlg)


def test_stale_after_edit(qtbot, opened, dialog, records_path: Path) -> None:
    s = opened(records_path, edit=True)
    dlg = dialog(s)
    find(qtbot, dlg, "city")
    got: list = []
    assert s.edit.set_value(NodeRef(0, ("city",)), '"Xiva"', on_done=got.append) is not False
    qtbot.waitUntil(lambda: bool(got) and dlg.stale() == "data", timeout=TIMEOUT)
    assert dlg.stale_label.isVisibleTo(dlg) and dlg.stale_label.text() == t("gui.dupesdialog.stale.data")
    for button in (dlg.show_button, dlg.export_button):
        assert not button.isEnabled() and button.toolTip() == t("gui.dupesdialog.why.stale")
    assert dlg.show_duplicates() is None and dlg.reveal_position(4) is None
    assert dlg.remove_button.isEnabled()                         # it searches again (scope: all records)
    assert dlg.filter_group(0) is True                            # a value, not a record number
    wait_view(qtbot, s, 14)
    find(qtbot, dlg, "city")
    assert dlg.stale() is None and dlg.show_button.isEnabled()


def test_reopen_and_close(qtbot, opened, dialog, records_path: Path, gated) -> None:
    gate, started = gated
    s = opened(records_path)
    dlg = dialog(s)
    dlg.set_key("city")
    assert dlg.run() is not None
    qtbot.waitUntil(started.is_set, timeout=TIMEOUT)
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen()
    gate.set()
    assert dlg.status() == "idle" and dlg.status_text() == t("gui.dupesdialog.status.reopened")
    find(qtbot, dlg, "city")
    with qtbot.waitSignal(dlg.finished, timeout=TIMEOUT):
        s.close()                                                # closing the file closes the dialog
    assert DupesDialog.instance(s) is None


def test_show_dupes_dialog_registry_and_uzbek_texts(qtbot, opened, records_path: Path, lang_uz) -> None:
    s = opened(records_path)
    dlg = show_dupes_dialog(None, s, key="city", scope="all")
    try:
        assert DupesDialog.instance(s) is dlg and dlg.isVisible() and not dlg.isModal()
        assert dlg.key_text() == "city" and dlg.scope() == "all" and dlg.task is None    # nothing runs by itself
        assert show_dupes_dialog(None, s, key="id") is dlg and dlg.key_text() == "id"
        assert dlg.windowTitle() == f"Takroriy yozuvlar — {s.name}"
        assert dlg.run_button.text() == "Qidirish" and dlg.remove_button.text() == "Takroriylarni o'chirish…"
        assert dlg.show_button.text() == "Takroriylarni ko'rsatish"
        assert dlg.keep_buttons["first"].text() == "birinchi yozuv"
    finally:
        dlg.close()
    qtbot.waitUntil(lambda: DupesDialog.instance(s) is None, timeout=TIMEOUT)


# ----------------------------------------------------------------------------------
# document kinds and encodings
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt, opts, open_kw", KIND_CASES, ids=[f"{c[0]}-{i}" for i, c in enumerate(KIND_CASES)])
def test_document_kinds_and_encodings(qtbot, opened, dialog, tmp_path: Path, fmt, opts, open_kw) -> None:
    s = opened(write_kind(tmp_path, fmt, opts), **open_kw)
    assert s.doc.kind == fmt
    dlg = dialog(s)
    rep = find(qtbot, dlg, "city")
    assert (rep.groups, rep.duplicates, dlg.count_text("distinct")) == (4, 20, "4")
    assert dlg.group_key_text(2) == '"Ўзбекистон"' and dlg.group_positions(2) == [2, 6, 10, 14, 18, 22]
    with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT) as sig:
        dlg.reveal_position(6)
    assert sig.args[0] == NodeRef(6, ("city",))
    rep = find(qtbot, dlg, "v")
    assert rep.groups == 5 and [dlg.group_key_text(k) for k in range(5)] == \
        ["0.1", "1e400", "12345678901234567890", "-0.0", "1.10"]
    assert dlg.show_duplicates() is not None
    wait_view(qtbot, s, 19)
    assert sorted(s.record_at(k) for k in range(19)) == list(range(5, 24))


def test_menu_key_and_copy_in_the_groups(qtbot, opened, dialog, records_path: Path) -> None:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QContextMenuEvent
    from PySide6.QtWidgets import QApplication

    s = opened(records_path)
    dlg = dialog(s)
    dlg.show()
    qtbot.waitExposed(dlg)
    find(qtbot, dlg, "city")
    expected = json.dumps("O'zbekiston")
    group = dlg.groups.topLevelItem(3)
    dlg.groups.setFocus()
    dlg.groups.setCurrentItem(group)
    QApplication.sendEvent(dlg.groups, QContextMenuEvent(QContextMenuEvent.Reason.Keyboard, QPoint(2, 2), QPoint(2, 2)))
    menu = dlg.last_menu
    assert menu is not None and menu.actions()[1].toolTip() == dlg.group_key_text(3) == expected
    menu.close()
    qtbot.keyClick(dlg.groups, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == expected
    with qtbot.waitSignal(s.reveal_requested, timeout=TIMEOUT) as sig:
        dlg.groups.setCurrentItem(group.child(1))
        qtbot.keyClick(dlg.groups, Qt.Key.Key_Return)
    assert sig.args[0] == NodeRef(7, ("city",))
