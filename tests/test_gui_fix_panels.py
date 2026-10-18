"""Fix round: filter panel, sort dialog, schema view, settings dialog.

* P-F2: the schema (field lists) is asked for only when needed: a hidden filter panel, a
  sort dialog not shown yet, an inactive schema view compute nothing; never for a single
  document; a sort dialog opened while indexing gets its fields when indexing ends (L8);
* M4: changing an operator reads the shown value again for the new operator;
* L7: builder errors stay after whitespace-only text edits; incomplete rows survive a
  language round trip;
* T9: preset file operations never run on the GUI thread; a reopen that drops the filter
  is shown in the panel unless the draft has unapplied edits;
* C11: the presence bar keeps a visible color on a selected row;
* L5: settings: the cache folder must be absolute, on an existing drive and not a file;
  saving an untouched dialog changes nothing; JSONRESHAPE_LANG keeps choosing the
  language; the cache measurement never calls back into a closed dialog.
"""

from __future__ import annotations

import json
import string
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from jsonreshape import i18n  # noqa: E402
from jsonreshape import settings as core_settings  # noqa: E402
from jsonreshape.gui import fields, schemaview, settingsdialog, theme, widgets  # noqa: E402
from jsonreshape.gui.context import AppContext  # noqa: E402
from jsonreshape.gui.filterpanel import FilterPanel, _ValueEdit  # noqa: E402
from jsonreshape.gui.guistate import GuiState  # noqa: E402
from jsonreshape.gui.schemaview import SchemaView, presence_bar_colors  # noqa: E402
from jsonreshape.gui.session import DocSession, FilterSpec  # noqa: E402
from jsonreshape.gui.settingsdialog import SettingsDialog, cache_dir_problem, exact_size  # noqa: E402
from jsonreshape.gui.sortdialog import SortDialog  # noqa: E402
from jsonreshape.query import FilterPresets  # noqa: E402
from jsonreshape.settings import Settings, get_settings, set_settings  # noqa: E402

pytestmark = pytest.mark.gui

TIMEOUT = 30000


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
def schema_calls(monkeypatch):
    calls: list = []
    real = fields.get_schema

    def get_schema(session, callback=None, **kw):
        calls.append(session)
        return real(session, callback, **kw)

    monkeypatch.setattr(fields, "get_schema", get_schema)
    return calls


@pytest.fixture
def records_file(tmp_path: Path) -> Path:
    p = tmp_path / "recs.json"
    p.write_text(json.dumps([{"x": i, "s": "ab"[i % 2], "o": {"k": i}} for i in range(40)]), encoding="utf-8")
    return p


# ----------------------------------------------------------------------------------
# P-F2 / L8: field lists only when needed
# ----------------------------------------------------------------------------------
def test_hidden_filter_panel_computes_no_schema(qtbot, app, opened, records_file, tmp_path, schema_calls) -> None:
    s = opened(records_file)
    panel = FilterPanel(app)
    qtbot.addWidget(panel)
    panel.set_session(s)
    panel._on_index_finished()
    ed = panel.current_editor()
    row = ed.root.add_condition("x", "eq")
    assert schema_calls == []                           # hidden: nothing computed
    row.field.showPopup()                               # opening a field list asks for the fields
    row.field.hidePopup()
    assert schema_calls == [s]
    qtbot.waitUntil(lambda: "o.k" in ed.field_names(), timeout=TIMEOUT)
    # a single document: never, even when shown
    one = tmp_path / "one.json"
    one.write_text('{"a": {"b": 1}}', encoding="utf-8")
    s1 = opened(one)
    schema_calls.clear()
    panel.set_session(s1)
    panel.show()
    qtbot.waitExposed(panel)
    QApplication.processEvents()
    assert schema_calls == []
    panel.set_session(s)
    assert schema_calls == []                           # its fields are known already
    s2 = opened(records_file)
    panel.set_session(s2)                               # visible: asked at once
    assert schema_calls == [s2]


class _FakeSession(QObject):
    index_finished = Signal()

    def __init__(self, app) -> None:
        super().__init__()
        self.app = app
        self.doc = type("Doc", (), {"kind": "array"})()
        self.state = "indexing"
        self.sort_keys = []


def test_sort_dialog_asks_for_fields_when_shown_and_after_indexing(qtbot, app, monkeypatch) -> None:
    calls: list = []

    def get_schema(session, callback=None, **kw):
        calls.append(session.state)
        return object()

    monkeypatch.setattr(fields, "get_schema", get_schema)
    monkeypatch.setattr(fields, "field_paths", lambda schema, **kw: ["name", "age"])
    s = _FakeSession(app)
    dlg = SortDialog(s)
    qtbot.addWidget(dlg)
    assert calls == []                                  # not shown yet
    dlg.show()
    qtbot.waitExposed(dlg)
    assert calls == [] and dlg.field_names() == []      # still indexing
    s.state = "ready"
    s.index_finished.emit()
    assert calls == ["ready"] and dlg.field_names() == ["name", "age", "$index"]
    dlg.reject()
    s.index_finished.emit()
    assert calls == ["ready"]                           # a closed dialog asks nothing


def test_inactive_schema_view_computes_nothing(qtbot, app, opened, records_file, schema_calls) -> None:
    s = opened(records_file)
    v = SchemaView(s)
    qtbot.addWidget(v)
    s.index_finished.emit()
    assert schema_calls == []
    v.show()
    v.activate()
    assert schema_calls == [s]
    qtbot.waitUntil(lambda: v.schema is not None, timeout=TIMEOUT)
    v.shutdown()


# ----------------------------------------------------------------------------------
# M4 / L7: the builder
# ----------------------------------------------------------------------------------
def test_operator_change_reads_the_value_again(qtbot) -> None:
    v = _ValueEdit()
    qtbot.addWidget(v)
    v.set_value(5)
    assert v.value() == (True, 5)
    v.set_op("in")
    assert v.value() == (True, [5])
    v.set_op("regex")
    assert v.value() == (True, "5")
    v.set_op("regex")                                   # the same operator: nothing changes
    assert v.value() == (True, "5")
    v.set_op("eq")
    v.set_value([1, 2])
    v.set_op("gt")
    assert v.value() == (True, [1, 2])                  # the shown text "[1,2]" read again


def test_builder_state_after_text_edits(qtbot, app, opened, records_file) -> None:
    s = opened(records_file)
    panel = FilterPanel(app)
    qtbot.addWidget(panel)
    panel.set_session(s)
    ed = panel.current_editor()
    ed.set_text('s == "a"')
    row = ed.root.add_condition("s", "regex")
    row.v1.set_raw("[")
    assert ed.builder_error_text() and row.has_error()
    ed.expr.setPlainText(ed.text() + "  ")              # whitespace only
    ed.flush()
    assert ed.builder_error_text() and row.has_error() and ed.builder_dict() is None
    assert not panel.apply()                            # refused like before the edit
    # incomplete rows survive expr -> JMESPath -> expr
    ed.set_text("x > 3")
    ed.root.add_condition("s", "eq")                    # no value: incomplete
    rows = [(r.field_text(), r.op_name()) for r in ed.root.rows()]
    ed.lang.setCurrentIndex(ed.lang.findData("jmespath"))
    ed.flush()
    ed.lang.setCurrentIndex(ed.lang.findData("expr"))
    ed.flush()
    assert [(r.field_text(), r.op_name()) for r in ed.root.rows()] == rows and ed.state == "sync"


# ----------------------------------------------------------------------------------
# T9: presets off the GUI thread; the panel follows a reopen
# ----------------------------------------------------------------------------------
def test_preset_file_operations_run_off_the_gui_thread(qtbot, app, opened, records_file, monkeypatch) -> None:
    widgets_answer = []
    monkeypatch.setattr(widgets, "confirm_handler", lambda *a: (widgets_answer.append(a), True)[1])
    threads: list[bool] = []
    main = threading.main_thread()
    for name in ("save", "get", "delete", "names"):
        real = getattr(FilterPresets, name)

        def wrapped(self, *a, _real=real, **k):
            threads.append(threading.current_thread() is main)
            return _real(self, *a, **k)

        monkeypatch.setattr(FilterPresets, name, wrapped)
    s = opened(records_file)
    panel = FilterPanel(app)
    qtbot.addWidget(panel)
    panel.set_session(s)
    ed = panel.current_editor()
    ed.set_text("x > 3")
    assert panel.save_preset("p1")
    assert panel.load_preset("p1")                      # waits behind the save
    assert panel.presets_busy() and not panel.preset_load_button.isEnabled()
    qtbot.waitUntil(lambda: not panel.presets_busy(), timeout=TIMEOUT)
    assert panel.preset_names() == ["p1"] and ed.text() == "x > 3"
    assert panel.delete_preset("p1")
    qtbot.waitUntil(lambda: not panel.presets_busy(), timeout=TIMEOUT)
    assert panel.preset_names() == [] and threads and not any(threads)


def test_panel_follows_a_reopen_that_drops_the_filter(qtbot, app, opened, tmp_path) -> None:
    p = tmp_path / "wrapped.json"
    p.write_text(json.dumps({"a": [{"x": i} for i in range(10)], "b": [{"y": i} for i in range(5)]}),
                 encoding="utf-8")
    s = opened(p, record_path="a")
    panel = FilterPanel(app)
    qtbot.addWidget(panel)
    panel.set_session(s)
    ed = panel.current_editor()
    ed.set_text("x > 3")
    assert panel.apply()
    qtbot.waitUntil(lambda: not s.view_busy and s.filter is not None, timeout=TIMEOUT)
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen(record_path="b")
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert s.filter is None
    assert ed.text() == "" and not ed.dirty             # the applied draft follows the session
    # an unapplied edit is kept
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen(record_path="a")
    ed.set_text("x < 2")
    panel.apply()
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    ed.set_text("x < 5")                                # edited after applying
    with qtbot.waitSignal(s.index_finished, timeout=TIMEOUT):
        s.reopen(record_path="b")
    qtbot.waitUntil(lambda: not s.view_busy, timeout=TIMEOUT)
    assert ed.text() == "x < 5" and ed.dirty


# ----------------------------------------------------------------------------------
# C11: the presence bar on a selected row
# ----------------------------------------------------------------------------------
def test_presence_bar_stays_visible_on_a_selected_row(qapp) -> None:
    previous = theme.mode()
    theme.apply_theme(qapp, "dark")
    try:
        pal = qapp.palette()
        highlight = pal.color(pal.ColorRole.Highlight)
        selected_text = pal.color(pal.ColorRole.HighlightedText)
        track, fill = presence_bar_colors(True, selected_text)
        assert fill == selected_text and track.alpha() < 255

        def distance(a: QColor, b: QColor) -> int:
            return abs(a.red() - b.red()) + abs(a.green() - b.green()) + abs(a.blue() - b.blue())

        assert distance(fill, highlight) > 150          # the bar is not drawn in the selection color
        normal_track, normal_fill = presence_bar_colors(False, selected_text)
        assert normal_fill == QColor(theme.color("accent"))
    finally:
        theme.apply_theme(qapp, previous)


# ----------------------------------------------------------------------------------
# L5: settings
# ----------------------------------------------------------------------------------
@pytest.fixture
def restore_settings():
    before = get_settings()
    lang = i18n.get_language()
    yield
    set_settings(before)
    i18n.set_language(lang)


def _missing_drive() -> str | None:
    if sys.platform != "win32":
        return None
    import ctypes

    for letter in reversed(string.ascii_uppercase):
        if ctypes.windll.kernel32.GetDriveTypeW(f"{letter}:\\") == 1:
            return f"{letter}:"
    return None


def test_exact_sizes() -> None:
    assert exact_size("9007199254740993") == 9007199254740993
    assert exact_size("1.5GB") == 1536 * 1024 * 1024
    assert exact_size("64k") == 65536
    for bad in ("", "abc", "-5MB", "nan", "inf"):
        with pytest.raises(ValueError):
            exact_size(bad)


def test_cache_folder_must_be_a_usable_folder(qtbot, app, tmp_path, restore_settings) -> None:
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    assert cache_dir_problem("") is None and cache_dir_problem(str(tmp_path / "new")) is None
    assert cache_dir_problem("relcache") is not None
    assert cache_dir_problem(str(a_file)) is not None
    missing = _missing_drive()
    if missing is not None:
        assert cache_dir_problem(missing + "\\cache") is not None
    dlg = SettingsDialog(app)
    qtbot.addWidget(dlg)
    dlg.cache_dir_edit.setText("relcache")
    assert "cache_dir" in dlg.errors() and not dlg.save_button.isEnabled() and not dlg.save()
    dlg.cache_dir_edit.setText(str(a_file))
    assert "cache_dir" in dlg.errors()
    dlg.cache_dir_edit.setText(str(tmp_path / "kesh"))
    assert dlg.errors() == {} and dlg.save()
    assert get_settings().cache_dir == str(tmp_path / "kesh")
    dlg.reject()


def test_saving_an_untouched_dialog_changes_nothing(qtbot, app, restore_settings, monkeypatch) -> None:
    custom = Settings(sample_size=3_000_000_000, large_node_children=5_000_000_000,
                      cache_limit_bytes=(1 << 53) + 1, sort_memory_bytes=1536 * 1024 * 1024 + 1,
                      large_node_bytes=3 * (1 << 40) // 7, memory_limit_bytes=1234567)
    set_settings(custom)
    app.state.set("mono_size", 11.5)
    app.state.set("mono_font", "NoSuchFontXYZ")
    saved: list[Settings] = []
    monkeypatch.setattr(Settings, "save", lambda self, path=None: saved.append(self))
    dlg = SettingsDialog(app)
    qtbot.addWidget(dlg)
    assert dlg.errors() == {}
    assert dlg.save()
    assert saved == [custom] and get_settings() == custom
    assert app.state.get("mono_size") == 11.5 and app.state.get("mono_font") == "NoSuchFontXYZ"
    # edited values are taken as typed
    dlg._fields["sample_size"].setValue(1000)
    dlg.mono_size_spin.setValue(12)
    assert dlg.save()
    assert saved[-1].sample_size == 1000 and saved[-1].large_node_children == 5_000_000_000
    assert app.state.get("mono_size") == 12
    dlg.reject()


def test_environment_language_keeps_winning(qtbot, app, restore_settings, monkeypatch) -> None:
    monkeypatch.setenv("JSONRESHAPE_LANG", "en")
    i18n.set_language("en")
    set_settings(Settings(language="uz"))
    monkeypatch.setattr(Settings, "save", lambda self, path=None: None)
    dlg = SettingsDialog(app)
    qtbot.addWidget(dlg)
    assert dlg.language_combo.currentData() == "uz"
    assert dlg.note_label.isVisibleTo(dlg) and "JSONRESHAPE_LANG" in dlg.note_label.text()
    dlg.theme_combo.setCurrentIndex(dlg.theme_combo.findData("light"))
    assert dlg.save()
    assert i18n.get_language() == "en" and get_settings().language == "uz"
    dlg.reject()


def test_cache_measurement_never_reaches_a_closed_dialog(qtbot, app, monkeypatch) -> None:
    release = threading.Event()
    real = settingsdialog.cache_usage

    def slow(settings=None):
        release.wait(5)
        return real(settings)

    monkeypatch.setattr(settingsdialog, "cache_usage", slow)
    dlg = SettingsDialog(app)
    dlg.deleteLater()                                   # destroyed with its parent, no reject()
    QApplication.sendPostedEvents(None, 0)
    QApplication.processEvents()
    release.set()
    assert app.tasks.wait_idle(10)
    assert app.errors == []                             # no callback into the deleted dialog


def test_settings_module_has_no_parse_size_rounding() -> None:
    assert core_settings.Settings.from_dict({"cache_limit_bytes": exact_size("9007199254740993")},
                                            strict=True).cache_limit_bytes == 9007199254740993
