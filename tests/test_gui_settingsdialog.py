"""SettingsDialog (jsonreshape/gui/settingsdialog.py; ARCHITECTURE §4, §11.8; AGENT_PROMPT §11).

Load -> save round trip of every core setting and the GUI preferences, sizes typed with
units (exact text both ways), invalid values marked inline with "Saqlash" disabled, the
theme applied live and reverted on cancel, the language switch (note + set_language),
"restore defaults", the cache size measured in the background, save errors reported.
"""

from __future__ import annotations

import os

import random
from pathlib import Path

import pytest
from PySide6.QtGui import QFont, QPalette
from PySide6.QtWidgets import QApplication, QDialog

from jsonreshape import i18n
from jsonreshape.errors import UserInputError
from jsonreshape.gui import theme
from jsonreshape.gui.context import AppContext
from jsonreshape.gui.guistate import GuiState
from jsonreshape.gui.settingsdialog import SettingsDialog, cache_usage, size_text
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
                      language="uz", cache_dir=kesh)
    set_settings(custom)
    app.state.set("theme", "light")
    app.state.set("mono_size", 13)
    dlg = make()
    values = dlg.values()
    assert values["sample_size"] == 777 and values["cache_dir"] == kesh
    assert values["memory_limit_bytes"] == "768MB" and values["cache_limit_bytes"] == "20GB"
    assert values["head_bytes"] == "4MB" and values["export_indent"] == 3 and values["csv_delimiter"] == ";"
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
    dlg.mono_size_spin.setValue(12)
    dlg.ui_size_spin.setValue(11)
    assert dlg.errors() == {}
    with qtbot.waitSignal(dlg.saved, timeout=TIMEOUT):
        dlg.accept()
    assert dlg.result() == QDialog.DialogCode.Accepted
    want = Settings(sample_size=2500, cache_dir="C:\\boshqa kesh", cache_limit_bytes=5 * GiB, memory_limit_bytes=GiB,
                    sort_memory_bytes=300 * MiB, large_node_bytes=1536 * MiB, large_node_children=20000,
                    head_bytes=65536, language="en", export_indent="\t", csv_bom=False, csv_delimiter=";",
                    array_join_sep=" | ", xlsx_max_rows=1000)
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
    set_settings(Settings(sample_size=5, csv_bom=False, xlsx_max_rows=10, memory_limit_bytes=64 * MiB))
    app.state.set("mono_size", 20)
    app.state.set("theme", "dark")
    dlg = make()
    dlg.restore_defaults()
    assert dlg.settings() == Settings()
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
