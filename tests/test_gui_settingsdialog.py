"""SettingsDialog (jsonreshape/gui/settingsdialog.py; ARCHITECTURE §4, §11.8, §12.9; AGENT_PROMPT §11).

Load -> save round trip of every core setting and the GUI preferences, sizes typed with
units (exact text both ways), invalid values marked inline with "Saqlash" disabled, the
theme applied live and reverted on cancel, the language switch (note + set_language),
"restore defaults", the cache size measured in the background, save errors reported; the
editing settings on the General tab (where unsaved changes are kept, the .bak copy, undo
steps) and a guard that ``values()`` covers every setting (a missing one would be reset by
every save).
"""

from __future__ import annotations

import dataclasses
import os
import random
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QPalette
from PySide6.QtWidgets import QApplication, QDialog

from jsonreshape import appdirs, i18n
from jsonreshape.errors import UserInputError
from jsonreshape.gui import theme
from jsonreshape.gui.context import AppContext
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.settingsdialog import (
    UNDO_LIMIT_MAX,
    SettingsDialog,
    cache_usage,
    edits_folder_text,
    edits_location_text,
    size_text,
)
from jsonreshape.i18n import t
from jsonreshape.settings import Settings, get_settings, set_settings
from jsonreshape.tasks import parse_size

pytestmark = pytest.mark.gui

TIMEOUT = 30000
MiB = 1 << 20
GiB = 1 << 30


@pytest.fixture
def app(qapp, tmp_path: Path):
    ctx = AppContext(state=GuiState.load(tmp_path / "gui_state.json"), headless=True)
    ctx.tasks.set_poll_interval(10)
    ctx.errors = []
    ctx.error_reported.connect(ctx.errors.append)
    yield ctx
    assert ctx.tasks.shutdown(30)


@pytest.fixture
def restore_globals(qapp):
    """Theme, application font and language are process-wide: restore them afterwards."""
    palette = QPalette(qapp.palette())
    style = qapp.style().name()
    font = QFont(QApplication.font())
    lang = i18n.get_language()
    yield qapp
    theme._reset_for_tests()
    try:
        qapp.styleHints().unsetColorScheme()
    except AttributeError:  # pragma: no cover
        pass
    qapp.setStyle(style)
    qapp.setPalette(palette)
    theme.invalidate()
    QApplication.setFont(font)
    i18n.set_language(lang)


@pytest.fixture
def make(qtbot, app):
    def factory() -> SettingsDialog:
        dlg = SettingsDialog(app)
        qtbot.addWidget(dlg)
        return dlg

    return factory


def field(dlg: SettingsDialog, name: str):
    return dlg._fields[name]


# ----------------------------------------------------------------------------------------
# sizes
# ----------------------------------------------------------------------------------------
def test_size_text_is_exact_and_short() -> None:
    assert size_text(20 * GiB) == "20GB"
    assert size_text(512 * MiB) == "512MB"
    assert size_text(1536 * MiB) == "1.5GB"
    assert size_text(64 * 1024) == "64KB"
    assert size_text(1000) == "1000"
    assert size_text(0) == "0"
    assert size_text(3 * (1 << 40)) == "3TB"
    rng = random.Random(7)
    for _ in range(2000):
        n = rng.choice([rng.randrange(1, 10**6), rng.randrange(1, 1 << 45), rng.randrange(1, 4096) * MiB,
                        rng.randrange(1, 64) * GiB + rng.randrange(0, 3) * 512 * MiB])
        text = size_text(n)
        assert parse_size(text) == n, (n, text)
        assert len(text) <= len(str(n)) + 2


# ----------------------------------------------------------------------------------------
# load / save
# ----------------------------------------------------------------------------------------
def test_shows_the_current_settings(qtbot, app, make, lang_uz) -> None:
    kesh = os.path.join(os.path.abspath(os.sep), "kesh")     # on an existing drive (validated)
    custom = Settings(sample_size=777, memory_limit_bytes=768 * MiB, csv_delimiter=";", export_indent=3,
                      language="uz", cache_dir=kesh, edits_location="beside", backup_on_save=False, undo_limit=777)
    set_settings(custom)
    app.state.set("theme", "light")
    app.state.set("mono_size", 13)
    dlg = make()
    values = dlg.values()
    assert values["sample_size"] == 777 and values["cache_dir"] == kesh
    assert values["memory_limit_bytes"] == "768MB" and values["cache_limit_bytes"] == "20GB"
    assert values["head_bytes"] == "4MB" and values["export_indent"] == 3 and values["csv_delimiter"] == ";"
    assert values["edits_location"] == "beside" and values["backup_on_save"] is False and values["undo_limit"] == 777
    assert dlg.edits_location_combo.currentText() == "Fayl yonida (<fayl nomi>.jredit)"
    assert not dlg.backup_check.isChecked() and field(dlg, "undo_limit").value() == 777
    assert dlg.indent_combo.currentText() == "3 bo'shliq"
    assert dlg.settings() == custom
    assert dlg._hints["memory_limit_bytes"].text() == "= 805 306 368 bayt"
    assert dlg.gui_values() == {"theme": "light", "mono_font": "", "mono_size": 13, "ui_size": 0}
    assert dlg.errors() == {} and dlg.save_button.isEnabled()


def test_save_round_trip(qtbot, app, make, restore_globals, monkeypatch) -> None:
    monkeypatch.delenv("JSONRESHAPE_LANG", raising=False)   # no override: the saved language is used
    dlg = make()
    changed: list[bool] = []
    app.settings_changed.connect(lambda: changed.append(True))
    field(dlg, "sample_size").setValue(2500)
    dlg.cache_dir_edit.setText("  C:\\boshqa kesh  ")
    field(dlg, "cache_limit_bytes").setText("5 GB")
    field(dlg, "memory_limit_bytes").setText("1g")
    field(dlg, "sort_memory_bytes").setText("300MB")
    field(dlg, "large_node_bytes").setText("1.5GB")
    field(dlg, "large_node_children").setValue(20000)
    field(dlg, "head_bytes").setText("65536")
    dlg.indent_combo.setCurrentIndex(2)          # tab
    dlg.bom_check.setChecked(False)
    dlg.delimiter_combo.setCurrentIndex(1)       # ;
    dlg.join_sep_edit.setText(" | ")
    field(dlg, "xlsx_max_rows").setValue(1000)
    dlg.language_combo.setCurrentIndex(1)        # English
    dlg.edits_location_combo.setCurrentIndex(dlg.edits_location_combo.findData("beside"))
    dlg.backup_check.setChecked(False)
    field(dlg, "undo_limit").setValue(5000)
    dlg.mono_size_spin.setValue(12)
    dlg.ui_size_spin.setValue(11)
    assert dlg.errors() == {}
    with qtbot.waitSignal(dlg.saved, timeout=TIMEOUT):
        dlg.accept()
    assert dlg.result() == QDialog.DialogCode.Accepted
    want = Settings(sample_size=2500, cache_dir="C:\\boshqa kesh", cache_limit_bytes=5 * GiB, memory_limit_bytes=GiB,
                    sort_memory_bytes=300 * MiB, large_node_bytes=1536 * MiB, large_node_children=20000,
                    head_bytes=65536, language="en", export_indent="\t", csv_bom=False, csv_delimiter=";",
                    array_join_sep=" | ", xlsx_max_rows=1000, edits_location="beside", backup_on_save=False,
                    undo_limit=5000)
    assert Settings.load() == want                  # written to settings.json
    assert get_settings() == want and app.settings == want
    assert changed == [True]
    assert app.tasks.wait_idle(10)                 # the GUI state is saved in the background
    state = GuiState.load(app.state.path)
    assert state.get("mono_size") == 12 and state.get("ui_size") == 11 and state.get("theme") == "system"
    assert QApplication.font().pointSizeF() == 11.0
    assert i18n.get_language() == "en" and t("gui.settings.title") == "Settings"
    # reopened: the dialog shows what was saved
    again = make()
    assert again.settings() == want and again.values()["large_node_bytes"] == "1.5GB"
    assert again.gui_values()["mono_size"] == 12


def test_invalid_values_are_rejected_inline(qtbot, app, make, lang_uz) -> None:
    before = Settings.load()
    dlg = make()
    bad = {
        "memory_limit_bytes": "abc",
        "sort_memory_bytes": "0",
        "cache_limit_bytes": "-5MB",
        "head_bytes": "",
        "large_node_bytes": "1e400TB",
    }
    for name, text in bad.items():
        field(dlg, name).setText(text)
    dlg.delimiter_combo.setEditText('"')
    dlg.join_sep_edit.setText("")
    errors = dlg.errors()
    assert set(errors) == set(bad) | {"csv_delimiter", "array_join_sep"}
    assert "Xotira chegarasi" in errors["memory_limit_bytes"]
    for name in errors:
        widget = field(dlg, name)
        target = widget.lineEdit() if hasattr(widget, "lineEdit") and widget.lineEdit() is not None else widget
        assert target.property("invalid") is True, name
    assert not dlg.save_button.isEnabled() and dlg.error_label.isVisibleTo(dlg)
    assert dlg._hints["memory_limit_bytes"].text() == ""
    with pytest.raises(UserInputError):
        dlg.settings()
    dlg.accept()                                    # refused: nothing saved, the dialog stays
    assert dlg.result() != QDialog.DialogCode.Accepted and Settings.load() == before
    # fixing the values makes the dialog valid again
    for name in bad:
        field(dlg, name).setText("64MB")
    dlg.delimiter_combo.setEditText("Tab")
    dlg.join_sep_edit.setText(";")
    assert dlg.errors() == {} and dlg.save_button.isEnabled()
    assert dlg.values()["csv_delimiter"] == "\t"
    assert field(dlg, "memory_limit_bytes").property("invalid") is False


def test_theme_is_applied_live_and_cancel_reverts_it(qtbot, app, make, restore_globals) -> None:
    app.state.set("theme", "light")
    theme.apply_theme(QApplication.instance(), "light")
    dlg = make()
    assert dlg.theme_combo.currentData() == "light"
    with qtbot.waitSignal(theme.notifier().theme_changed, timeout=TIMEOUT):
        dlg.theme_combo.setCurrentIndex(dlg.theme_combo.findData("dark"))
    assert theme.mode() == "dark" and theme.current_scheme() == "dark"
    assert QApplication.instance().palette().color(QPalette.ColorRole.Window).name() == "#0e1117"
    dlg.reject()
    assert theme.mode() == "light"
    assert QApplication.instance().palette().color(QPalette.ColorRole.Window).name() == "#f2f4f7"
    assert app.state.get("theme") == "light"
    # saved: kept
    dlg = make()
    dlg.theme_combo.setCurrentIndex(dlg.theme_combo.findData("dark"))
    dlg.accept()
    assert app.tasks.wait_idle(10)
    assert theme.mode() == "dark" and GuiState.load(app.state.path).get("theme") == "dark"


def test_language_note_and_switch(qtbot, app, make, restore_globals, monkeypatch) -> None:
    monkeypatch.delenv("JSONRESHAPE_LANG", raising=False)   # no override: the setting chooses
    i18n.set_language("uz")
    dlg = make()
    assert not dlg.note_label.isVisibleTo(dlg)
    dlg.language_combo.setCurrentIndex(dlg.language_combo.findData("en"))
    assert dlg.note_label.isVisibleTo(dlg) and "qayta" in dlg.note_label.text()
    dlg.language_combo.setCurrentIndex(dlg.language_combo.findData("uz"))
    assert not dlg.note_label.isVisibleTo(dlg)
    dlg.language_combo.setCurrentIndex(dlg.language_combo.findData("en"))
    assert dlg.save()
    assert i18n.get_language() == "en" and get_settings().language == "en"


def test_restore_defaults_fills_the_form(qtbot, app, make, restore_globals) -> None:
    set_settings(Settings(sample_size=5, csv_bom=False, xlsx_max_rows=10, memory_limit_bytes=64 * MiB,
                          edits_location="beside", backup_on_save=False, undo_limit=300_000))
    app.state.set("mono_size", 20)
    app.state.set("theme", "dark")
    dlg = make()
    dlg.restore_defaults()
    assert dlg.settings() == Settings()
    assert dlg.edits_location_combo.currentData() == "appdata" and dlg.backup_check.isChecked()
    assert field(dlg, "undo_limit").value() == 1000 and dlg.values()["undo_limit"] == 1000
    assert dlg.gui_values() == {"theme": "system", "mono_font": "", "mono_size": 0, "ui_size": 0}
    assert get_settings().sample_size == 5            # not saved yet
    dlg.reject()


def test_mono_font_choice(qtbot, app, make) -> None:
    dlg = make()
    assert dlg.mono_default_check.isChecked() and not dlg.mono_font_combo.isEnabled()
    assert dlg.gui_values()["mono_font"] == ""
    dlg.mono_default_check.setChecked(False)
    assert dlg.mono_font_combo.isEnabled()
    family = dlg.mono_font_combo.currentFont().family()
    assert family and dlg.gui_values()["mono_font"] == family
    assert dlg.save()
    assert app.tasks.wait_idle(10)
    assert GuiState.load(app.state.path).get("mono_font") == family
    again = make()
    assert not again.mono_default_check.isChecked() and again.gui_values()["mono_font"] == family


def test_cache_size_is_measured_in_the_background(qtbot, app, make, tmp_path, lang_uz) -> None:
    folder, size = cache_usage()
    blob = Path(folder) / "fake-index.bin"
    blob.write_bytes(b"x" * 300_000)
    sub = Path(folder) / "sub"
    sub.mkdir(exist_ok=True)
    (sub / "more.bin").write_bytes(b"y" * 1000)
    assert cache_usage() == (folder, size + 301_000)
    dlg = SettingsDialog(app)
    qtbot.addWidget(dlg)
    with qtbot.waitSignal(dlg.cache_measured, timeout=TIMEOUT):
        pass
    assert dlg.cache_folder == folder and dlg.cache_bytes == size + 301_000
    assert dlg.cache_usage_label.text().startswith("Kesh hajmi:")
    assert dlg.open_cache_button.isEnabled()
    assert folder in dlg.cache_dir_edit.placeholderText()


def test_save_errors_are_reported(qtbot, app, make, monkeypatch) -> None:
    def fail(self, path=None):
        raise UserInputError("fileio.error.write", path="settings.json", detail="disk full")

    monkeypatch.setattr(Settings, "save", fail)
    dlg = make()
    field(dlg, "sample_size").setValue(42)
    dlg.accept()
    assert dlg.result() != QDialog.DialogCode.Accepted
    assert app.errors and isinstance(app.errors[-1], UserInputError)
    assert get_settings().sample_size != 42


# ----------------------------------------------------------------------------------------
# every setting has a widget; the editing settings (§12.9)
# ----------------------------------------------------------------------------------------
def test_values_cover_every_setting(qtbot, app, make, tmp_path, restore_globals) -> None:
    """A setting missing from ``values()`` would be reset to its default by every save: each
    field is loaded with a non-default value and must come back from the form unchanged."""
    custom = Settings(sample_size=777, cache_dir=str(tmp_path / "kesh"), cache_limit_bytes=3 * GiB,
                      memory_limit_bytes=768 * MiB, sort_memory_bytes=300 * MiB, large_node_bytes=10 * MiB,
                      large_node_children=1234, head_bytes=64 * 1024, language="en", export_indent=4,
                      csv_bom=False, csv_delimiter=";", array_join_sep=" | ", xlsx_max_rows=1000,
                      edits_location="beside", backup_on_save=False, undo_limit=250,
                      diff_align_max_bytes=1536 * MiB)
    names = [f.name for f in dataclasses.fields(Settings)]
    default = Settings()
    # a new setting must be added here (and to the dialog) with a non-default value
    assert [n for n in names if getattr(custom, n) == getattr(default, n)] == []
    dlg = make()
    assert sorted(dlg.values()) == sorted(names)
    assert set(dlg._fields) >= set(names)          # every setting has a widget that can be marked
    dlg.load(custom)
    assert dlg.errors() == {} and dlg.settings() == custom
    saved: list[Settings] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Settings, "save", lambda self, path=None: saved.append(self))
        assert dlg.save()
    assert saved == [custom] and get_settings() == custom
    dlg.reject()


def test_editing_settings_on_the_general_tab(qtbot, app, make, lang_uz, _isolated_app_home, tmp_path,
                                            monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))     # the test home is not inside it
    dlg = make()
    general = dlg.tabs.widget(0)
    combo, check, spin = dlg.edits_location_combo, dlg.backup_check, field(dlg, "undo_limit")
    for widget in (combo, check, spin):
        assert general.isAncestorOf(widget)
    assert [combo.itemData(i) for i in range(combo.count())] == ["appdata", "beside"]
    assert combo.itemText(0) == f"Dastur papkasida ({_isolated_app_home / 'edits'})"
    assert combo.itemText(1) == "Fayl yonida (<fayl nomi>.jredit)"
    assert check.text() == "Saqlashda .bak zaxira nusxasini qoldirish"
    form = general.layout()
    assert form.labelForField(combo).text() == "Saqlanmagan o'zgarishlar joyi:"
    assert form.labelForField(spin).text() == "Ortga qaytarish (undo) qadamlari:"
    assert (spin.minimum(), spin.maximum(), UNDO_LIMIT_MAX) == (200, 100000, 100000)
    # defaults
    assert combo.currentData() == "appdata" and check.isChecked() and spin.value() == 1000
    # tooltips are plain text (a "<name>" would be taken for a rich-text tag)
    for widget, key in ((combo, "gui.settings.edits_location.tip"), (check, "gui.settings.backup_on_save.tip"),
                        (spin, "gui.settings.undo_limit.tip")):
        assert widget.toolTip() == t(key) and "<" not in widget.toolTip()
    assert "data.json.bak" in check.toolTip() and "Ctrl+Z" in spin.toolTip()
    # editing them is validated like the rest; the spin box keeps the minimum of 200
    spin.setValue(5)
    assert spin.value() == 200 and dlg.errors() == {}
    combo.setCurrentIndex(1)
    check.setChecked(False)
    assert dlg.settings() == dataclasses.replace(get_settings(), edits_location="beside", backup_on_save=False,
                                                 undo_limit=200)
    dlg.reject()
    assert get_settings() == Settings()            # cancelled: nothing changed


def test_editing_settings_in_english(qtbot, app, make, lang_en, _isolated_app_home, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    dlg = make()
    combo = dlg.edits_location_combo
    assert combo.itemText(0) == f"App folder ({_isolated_app_home / 'edits'})"
    assert combo.itemText(1) == "Next to the file (<name>.jredit)"
    assert dlg.backup_check.text() == "Keep a .bak copy when saving"
    assert dlg.tabs.widget(0).layout().labelForField(field(dlg, "undo_limit")).text() == "Undo steps:"
    dlg.reject()


def test_a_long_app_folder_does_not_widen_the_dialog(qtbot, app, make, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    short = make()
    long_home = tmp_path.joinpath(*(["juda uzun papka nomi Тошкент"] * 8))
    monkeypatch.setenv("JSONRESHAPE_HOME", str(long_home))
    dlg = make()
    combo = dlg.edits_location_combo
    assert str(long_home / "edits") in combo.itemText(0)
    assert combo.itemData(0, Qt.ItemDataRole.ToolTipRole) == combo.itemText(0)   # readable in full
    assert dlg.minimumSizeHint().width() <= short.minimumSizeHint().width()
    assert dlg.sizeHint().width() <= short.sizeHint().width()
    short.reject()
    dlg.reject()


def test_undo_limit_beyond_the_spin_range_is_kept(qtbot, app, make, monkeypatch, restore_globals) -> None:
    """A larger value from the settings file (the core has no upper bound) survives saving
    the dialog untouched; editing the field takes the edited value."""
    custom = Settings(undo_limit=250_000)
    set_settings(custom)
    saved: list[Settings] = []
    monkeypatch.setattr(Settings, "save", lambda self, path=None: saved.append(self))
    dlg = make()
    spin = field(dlg, "undo_limit")
    assert spin.value() == UNDO_LIMIT_MAX and dlg.values()["undo_limit"] == 250_000
    assert dlg.errors() == {} and dlg.save()
    assert saved == [custom] and get_settings().undo_limit == 250_000
    spin.setValue(3000)
    assert dlg.values()["undo_limit"] == 3000 and dlg.save()
    assert saved[-1].undo_limit == 3000
    dlg.reject()


def test_diff_align_limit_on_the_advanced_tab(qtbot, app, make, lang_uz, monkeypatch, restore_globals) -> None:
    """Stage 5 (§14.0): the largest files compared in full without a key -- a size row on the
    Advanced tab (units, exact bytes, validation, defaults)."""
    set_settings(Settings(diff_align_max_bytes=1 << 30))
    dlg = make()
    edit = field(dlg, "diff_align_max_bytes")
    advanced = dlg.tabs.widget(dlg.tabs.count() - 1)
    assert advanced.isAncestorOf(edit) and dlg.tabs.tabText(dlg.tabs.count() - 1) == t("gui.settings.tab.advanced")
    form = advanced.layout()
    labels = [form.labelForField(form.itemAt(r, form.ItemRole.FieldRole).layout()) for r in range(form.rowCount())
              if form.itemAt(r, form.ItemRole.FieldRole) is not None
              and form.itemAt(r, form.ItemRole.FieldRole).layout() is not None
              and form.itemAt(r, form.ItemRole.FieldRole).layout().indexOf(edit) >= 0]
    assert [w.text() for w in labels] == ["Kalitsiz to'liq taqqoslanadigan fayl hajmi chegarasi:"]
    assert edit.text() == "1GB" and edit.toolTip() == t("gui.settings.diff_align_max_bytes.tip")
    assert dlg._hints["diff_align_max_bytes"].text() == "= 1 073 741 824 bayt"
    edit.setText("0")
    assert "diff_align_max_bytes" in dlg.errors() and not dlg.save_button.isEnabled()
    edit.setText("300MB")
    assert dlg.errors() == {} and dlg.settings().diff_align_max_bytes == 300 * MiB
    saved: list[Settings] = []
    monkeypatch.setattr(Settings, "save", lambda self, path=None: saved.append(self))
    assert dlg.save() and saved[-1].diff_align_max_bytes == 300 * MiB
    assert get_settings().diff_align_max_bytes == 300 * MiB
    dlg.restore_defaults()
    assert edit.text() == "200MB" and dlg.settings().diff_align_max_bytes == 200 * MiB
    dlg.reject()


def test_edits_folder_text(tmp_path, monkeypatch, _isolated_app_home) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))      # the test home is not inside it
    assert edits_folder_text() == str(_isolated_app_home / "edits")      # JSONRESHAPE_HOME: the real folder
    assert edits_location_text("beside") == t("gui.settings.edits_location.beside")
    assert edits_location_text("appdata") == t("gui.settings.edits_location.appdata",
                                               path=str(_isolated_app_home / "edits"))
    if sys.platform != "win32":
        return
    # the Windows default is named the way the user knows it
    local = tmp_path / "Local Тошкент"
    monkeypatch.delenv("JSONRESHAPE_HOME")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    appdirs._reset_for_tests()
    try:
        assert edits_folder_text() == "%LOCALAPPDATA%\\jsonreshape\\edits"
        monkeypatch.setenv("LOCALAPPDATA", str(local) + "\\")                 # a trailing separator
        assert edits_folder_text() == "%LOCALAPPDATA%\\jsonreshape\\edits"
        monkeypatch.setenv("JSONRESHAPE_HOME", str(local / "Boshqa"))          # moved, but still inside it
        assert edits_folder_text() == "%LOCALAPPDATA%\\Boshqa\\edits"
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Loc"))
        monkeypatch.setenv("JSONRESHAPE_HOME", str(tmp_path / "Locx"))         # only the name starts alike
        assert edits_folder_text() == str(tmp_path / "Locx" / "edits")
    finally:
        appdirs._reset_for_tests()


def test_a_fixed_field_gets_its_own_tooltip_back(qtbot, app, make, lang_uz) -> None:
    dlg = make()
    edit = field(dlg, "memory_limit_bytes")
    tip = t("gui.settings.memory_limit.tip")
    assert edit.toolTip() == tip
    edit.setText("abc")
    assert edit.property("invalid") is True and edit.toolTip() == dlg.errors()["memory_limit_bytes"] != tip
    edit.setText("-1")                              # still invalid: still the error text
    assert edit.toolTip() == dlg.errors()["memory_limit_bytes"]
    edit.setText("64MB")
    assert edit.property("invalid") is False and edit.toolTip() == tip
    combo = dlg.delimiter_combo                     # an editable combo marks its line edit
    combo.setEditText('"')
    assert combo.lineEdit().toolTip() == dlg.errors()["csv_delimiter"]
    combo.setEditText(";")
    assert combo.lineEdit().toolTip() == "" and combo.toolTip() == t("gui.export.delimiter.tip")
    dlg.reject()
