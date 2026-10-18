"""Settings dialog: docs/ARCHITECTURE.md §4, §11.8; AGENT_PROMPT §11 "Sozlamalar".

:class:`SettingsDialog` edits the core :class:`~jsonreshape.settings.Settings` (sample size,
cache folder and its size limit, memory limit, sort memory, large-node thresholds and the
format-detection head size in an advanced tab, export defaults, language) and the GUI
preferences kept in :class:`~jsonreshape.gui.guistate.GuiState` (theme -- applied live
while the dialog is open and reverted on cancel --, monospace font family and size, UI
font size).

Byte sizes are typed as text with units (``512MB``, ``20GB``; binary multiples, the syntax
of :func:`jsonreshape.tasks.parse_size`, read exactly -- no float rounding of big sizes)
and shown in the shortest exact form, with the exact number of bytes next to them. Every
field is validated through ``Settings.from_dict(..., strict=True)`` on each edit (the
cache folder also must be an absolute path on an existing drive and not a file): invalid
fields are marked inline and "Saqlash" is disabled while anything is invalid. Saving a
dialog nobody changed keeps every value as it was (numbers beyond the spin boxes' range,
fractional font sizes, an unknown font family).

Saving: ``Settings.save()``, ``set_settings(new)``, the GUI state (``app.save_state()``: in
the background), the UI font, ``i18n.set_language`` when the language changed (some texts
change only after a restart: the dialog says so) -- unless the ``JSONRESHAPE_LANG``
environment variable chooses the language, which wins (the dialog says that too) -- then
``app.settings_changed.emit()``. The size of the cache folder is computed in a silent task
that never calls back into a closed dialog.
"""

from __future__ import annotations

import logging
import os
import sys
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFontComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from jsonreshape import i18n
from jsonreshape.errors import JsonReshapeError, UserInputError
from jsonreshape.gui import copyfmt, theme
from jsonreshape.gui.guistate import DEFAULTS as GUI_DEFAULTS
from jsonreshape.gui.guistate import THEMES
from jsonreshape.gui.widgets import format_count, format_size, open_local_path
from jsonreshape.i18n import SUPPORTED_LANGUAGES, t
from jsonreshape.settings import XLSX_MAX_ROWS, Settings, get_settings, set_settings
from jsonreshape.tasks import parse_size

log = logging.getLogger(__name__)

__all__ = ["SIZE_FIELDS", "SettingsDialog", "cache_dir_problem", "cache_usage", "exact_size", "size_text"]

SIZE_FIELDS = ("cache_limit_bytes", "memory_limit_bytes", "sort_memory_bytes", "large_node_bytes", "head_bytes")
INT_MAX = 2**31 - 1
_UNITS = (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10))
_INDENTS: tuple[tuple[str, Any], ...] = (("2", 2), ("4", 4), ("tab", "\t"), ("compact", None))
_DELIMITERS: tuple[tuple[str, str], ...] = ((",", ","), (";", ";"), ("Tab", "\t"), ("|", "|"))
_LANGUAGES = (("uz", "O'zbekcha"), ("en", "English"))


def size_text(n: int) -> str:
    """The shortest text :func:`jsonreshape.tasks.parse_size` reads back as exactly ``n``
    bytes: ``512MB``, ``20GB``, ``1.5GB``, ``1000``."""
    n = int(n)
    for unit, factor in _UNITS:
        if n < factor:
            continue
        if n % factor == 0:
            return f"{n // factor}{unit}"
        for digits in (1, 2, 3):
            text = f"{n / factor:.{digits}f}".rstrip("0").rstrip(".") + unit
            try:
                if parse_size(text) == n and len(text) < len(str(n)):
                    return text
            except ValueError:  # pragma: no cover - always a number
                pass
    return str(n)


_SIZE_SUFFIXES = (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10),
                  ("T", 1 << 40), ("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10), ("B", 1))


def exact_size(text: str) -> int:
    """:func:`jsonreshape.tasks.parse_size` without float rounding: ``9007199254740993``
    stays that number, ``1.5GB`` is exactly 1610612736 (``ValueError`` when invalid)."""
    s = str(text).strip().upper().replace(" ", "")
    if not s:
        raise ValueError("empty size")
    mult = 1
    for suffix, factor in _SIZE_SUFFIXES:
        if s.endswith(suffix):
            mult = factor
            s = s[: -len(suffix)]
            break
    try:
        with localcontext() as ctx:
            ctx.prec = 80
            value = Decimal(s) * mult
    except InvalidOperation:
        raise ValueError(f"invalid size: {text!r}") from None
    if not value.is_finite():
        raise ValueError("size must be a finite number")
    if value < 0:
        raise ValueError("negative size")
    return int(value)


def _drive_missing(drive: str) -> bool:
    """A drive letter that does not exist (``GetDriveTypeW``: no file-system access, so a
    disconnected network drive never blocks the GUI thread)."""
    if sys.platform != "win32" or len(drive) != 2 or drive[1] != ":":
        return False
    import ctypes

    try:
        kind = ctypes.windll.kernel32.GetDriveTypeW(drive + "\\")
    except (AttributeError, OSError):
        return False
    return kind == 1          # DRIVE_NO_ROOT_DIR


def _drive_remote(drive: str) -> bool:
    if sys.platform != "win32":
        return False
    if drive.startswith(("\\\\", "//")):
        return True
    import ctypes

    try:
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4     # DRIVE_REMOTE
    except (AttributeError, OSError):
        return False


#: POSIX system folders refused as the cache folder (with everything under them).
_POSIX_SYSTEM_DIRS = ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/proc", "/sbin", "/sys",
                      "/usr", "/System")


def _norm_dir(path: str) -> str:
    """Lexically normalized, case-folded form of an absolute folder path (no file-system
    access: this runs on the GUI thread); the ``\\\\?\\`` prefix is dropped."""
    text = path
    if text.startswith(("\\\\?\\UNC\\", "//?/UNC/")):
        text = "\\\\" + text[8:]
    elif text.startswith(("\\\\?\\", "//?/")):
        text = text[4:]
    return os.path.normcase(os.path.normpath(text))


def system_folders() -> list[tuple[str, bool]]:
    """``(folder, with its subfolders)`` refused as the cache folder: on Windows
    ``%SystemRoot%`` / ``%WINDIR%``, ``%ProgramFiles%``, ``%ProgramFiles(x86)%``,
    ``%ProgramW6432%`` with everything under them, and ``%ProgramData%`` itself (a folder
    of one's own inside it is allowed: that is where programs keep shared data)."""
    if sys.platform != "win32":
        return [(d, True) for d in _POSIX_SYSTEM_DIRS]
    sysdrive = os.environ.get("SystemDrive") or "C:"
    out: list[tuple[str, bool]] = []
    for name, default, deep in (("SystemRoot", sysdrive + "\\Windows", True), ("WINDIR", None, True),
                                ("ProgramFiles", sysdrive + "\\Program Files", True),
                                ("ProgramFiles(x86)", sysdrive + "\\Program Files (x86)", True),
                                ("ProgramW6432", None, True),
                                ("ProgramData", sysdrive + "\\ProgramData", False)):
        value = (os.environ.get(name) or default or "").strip()
        if value and os.path.isabs(value):
            out.append((value, deep))
    return out


def _system_folder_problem(text: str) -> str | None:
    """A localized message when ``text`` (absolute) is a drive / share root or a system
    folder (the cache creates ``index`` and ``tmp`` folders in it)."""
    norm = _norm_dir(text)
    drive, rest = os.path.splitdrive(norm)
    if rest in ("", os.sep, "/", "\\"):
        return t("gui.settings.cache_dir.root", path=text)
    for folder, deep in system_folders():
        f = _norm_dir(folder)
        if norm == f or (deep and norm.startswith(f.rstrip("\\/") + os.sep)):
            return t("gui.settings.cache_dir.system", path=text, folder=folder)
    return None


def cache_dir_problem(path: str | None) -> str | None:
    """Why ``path`` cannot be the cache folder (a localized message), ``None`` when it can
    (empty = the default folder): it must be absolute, on an existing drive, not a file,
    not a drive root and not a system folder (:func:`system_folders`)."""
    text = (path or "").strip()
    if not text:
        return None
    drive, _rest = os.path.splitdrive(text)
    if not os.path.isabs(text) or (sys.platform == "win32" and not drive):
        return t("gui.settings.cache_dir.relative")
    problem = _system_folder_problem(text)
    if problem is not None:
        return problem
    if _drive_missing(drive):
        return t("gui.settings.cache_dir.no_drive", drive=drive)
    if not _drive_remote(drive):
        try:
            if os.path.isfile(text):
                return t("gui.settings.cache_dir.is_file")
        except (OSError, ValueError):
            return t("gui.settings.cache_dir.relative")
    return None


def env_language() -> str | None:
    """The language chosen by the ``JSONRESHAPE_LANG`` environment variable (it wins over
    the setting), ``None`` when it is not set to a supported language."""
    env = (os.environ.get("JSONRESHAPE_LANG") or "").strip().lower()
    return env if env in SUPPORTED_LANGUAGES else None


class _TaskOwner:
    """Owner of the dialog's tasks: detached when the dialog is destroyed (the connected
    function keeps no reference to the dialog)."""

    __slots__ = ("__weakref__",)


def _dir_size(path: Path) -> int:
    total = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def cache_usage(settings: Settings | None = None) -> tuple[str, int]:
    """``(index folder, bytes used by index files and prepared copies)`` for ``settings``
    (worker threads: walks the folders)."""
    from jsonreshape.appdirs import index_dir, work_dir

    settings = settings or get_settings()
    folder = index_dir(settings.cache_dir)
    total = _dir_size(folder)
    try:
        prepared = work_dir(settings.cache_dir) / "prepared"
        if prepared.is_dir():
            total += _dir_size(prepared)
    except (JsonReshapeError, OSError):
        log.debug("no work folder", exc_info=True)
    return str(folder), total


def _parse_delimiter(text: str) -> str:
    s = text if text in ("\t", " ") else text.strip()
    return "\t" if s.lower() in ("tab", "\\t") else s


class SettingsDialog(QDialog):
    """The settings window (see the module docstring). ``saved`` is emitted after a
    successful save (before the dialog closes)."""

    saved = Signal()
    cache_measured = Signal()

    def __init__(self, app: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self._loading = False
        self._errors: dict[str, str] = {}
        self._orig_theme = str(app.state.get("theme") or "system")
        self._applied_theme = self._orig_theme
        self._orig_language = get_settings().language
        self._env_language = env_language()
        self._loaded_ints: dict[str, int] = {}       # values beyond a spin box's range, until edited
        self._loaded_gui: dict[str, Any] = {}
        self._font_edited = False
        self._owner = _TaskOwner()
        self.destroyed.connect(lambda *_a, owner=self._owner, tasks=app.tasks: tasks.detach(owner))
        self.cache_folder: str | None = None
        self.cache_bytes: int | None = None
        self.setWindowTitle(t("gui.settings.title"))
        self.setSizeGripEnabled(True)
        self._fields: dict[str, QWidget] = {}
        self._hints: dict[str, QLabel] = {}
        self._build()
        self.load(get_settings())
        self._measure_cache()
        self.resize(640, self.sizeHint().height())

    # ================================================================================
    # construction
    # ================================================================================
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._general_tab(), t("gui.settings.tab.general"))
        self.tabs.addTab(self._export_tab(), t("gui.settings.tab.export"))
        self.tabs.addTab(self._view_tab(), t("gui.settings.tab.view"))
        self.tabs.addTab(self._advanced_tab(), t("gui.settings.tab.advanced"))
        layout.addWidget(self.tabs, 1)

        self.note_label = QLabel(self)
        self.note_label.setWordWrap(True)
        self.note_label.setTextFormat(Qt.TextFormat.PlainText)
        self.note_label.setVisible(False)
        layout.addWidget(self.note_label)
        self.error_label = QLabel(self)
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        self.defaults_button = QPushButton(t("gui.settings.defaults"), self)
        self.defaults_button.setToolTip(t("gui.settings.defaults.tip"))
        self.defaults_button.setAutoDefault(False)
        self.defaults_button.clicked.connect(self.restore_defaults)
        buttons.addWidget(self.defaults_button)
        buttons.addStretch(1)
        self.save_button = QPushButton(t("gui.settings.save"), self)
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.accept)
        buttons.addWidget(self.save_button)
        self.cancel_button = QPushButton(t("gui.common.cancel"), self)
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)
        self._style_labels()
        theme.notifier().theme_changed.connect(self._style_labels)

    def _form_page(self) -> tuple[QWidget, QFormLayout]:
        page = QWidget(self)
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        return page, form

    def _size_row(self, form: QFormLayout, name: str, tip_key: str) -> None:
        edit = QLineEdit(form.parentWidget())
        edit.setToolTip(t(tip_key))
        edit.setPlaceholderText(t("gui.settings.size.placeholder"))
        edit.setMaximumWidth(160)
        edit.textChanged.connect(self._changed)
        hint = QLabel(form.parentWidget())
        hint.setTextFormat(Qt.TextFormat.PlainText)
        row = QHBoxLayout()
        row.addWidget(edit)
        row.addWidget(hint, 1)
        form.addRow(t(f"settings.field.{name}") + ":", row)
        self._fields[name] = edit
        self._hints[name] = hint

    def _int_row(self, form: QFormLayout, name: str, lo: int, hi: int, tip_key: str) -> QSpinBox:
        spin = QSpinBox(form.parentWidget())
        spin.setRange(lo, hi)
        spin.setGroupSeparatorShown(True)
        spin.setToolTip(t(tip_key))
        spin.setMaximumWidth(160)
        spin.valueChanged.connect(lambda _v, n=name: self._int_edited(n))
        form.addRow(t(f"settings.field.{name}") + ":", spin)
        self._fields[name] = spin
        return spin

    def _general_tab(self) -> QWidget:
        page, form = self._form_page()
        self._int_row(form, "sample_size", 1, INT_MAX, "gui.settings.sample_size.tip")
        row = QHBoxLayout()
        self.cache_dir_edit = QLineEdit(page)
        self.cache_dir_edit.setPlaceholderText(t("gui.settings.cache_dir.default"))
        self.cache_dir_edit.setToolTip(t("gui.settings.cache_dir.tip"))
        self.cache_dir_edit.textChanged.connect(self._changed)
        row.addWidget(self.cache_dir_edit, 1)
        self.cache_browse_button = QPushButton(t("gui.settings.browse"), page)
        self.cache_browse_button.setAutoDefault(False)
        self.cache_browse_button.clicked.connect(self.choose_cache_dir)
        row.addWidget(self.cache_browse_button)
        self.cache_clear_button = QPushButton(t("gui.settings.cache_dir.clear"), page)
        self.cache_clear_button.setToolTip(t("gui.settings.cache_dir.clear.tip"))
        self.cache_clear_button.setAutoDefault(False)
        self.cache_clear_button.clicked.connect(lambda: self.cache_dir_edit.setText(""))
        row.addWidget(self.cache_clear_button)
        form.addRow(t("settings.field.cache_dir") + ":", row)
        self._fields["cache_dir"] = self.cache_dir_edit
        cache_row = QHBoxLayout()
        self.cache_usage_label = QLabel(t("gui.settings.cache.measuring"), page)
        cache_row.addWidget(self.cache_usage_label, 1)
        self.open_cache_button = QPushButton(t("gui.settings.cache.open"), page)
        self.open_cache_button.setAutoDefault(False)
        self.open_cache_button.setEnabled(False)
        self.open_cache_button.clicked.connect(self.open_cache_folder)
        cache_row.addWidget(self.open_cache_button)
        form.addRow("", cache_row)
        self._size_row(form, "cache_limit_bytes", "gui.settings.cache_limit.tip")
        self._size_row(form, "memory_limit_bytes", "gui.settings.memory_limit.tip")
        self._size_row(form, "sort_memory_bytes", "gui.settings.sort_memory.tip")
        self.language_combo = QComboBox(page)
        for code, name in _LANGUAGES:
            self.language_combo.addItem(name, code)
        self.language_combo.setToolTip(t("gui.settings.language.tip"))
        self.language_combo.currentIndexChanged.connect(self._changed)
        form.addRow(t("settings.field.language") + ":", self.language_combo)
        self._fields["language"] = self.language_combo
        return page

    def _export_tab(self) -> QWidget:
        page, form = self._form_page()
        self.indent_combo = QComboBox(page)
        for key, value in _INDENTS:
            self.indent_combo.addItem(t(f"gui.export.indent.{key}"), value)
        self.indent_combo.currentIndexChanged.connect(self._changed)
        form.addRow(t("settings.field.export_indent") + ":", self.indent_combo)
        self._fields["export_indent"] = self.indent_combo
        self.bom_check = QCheckBox(t("settings.field.csv_bom"), page)
        self.bom_check.toggled.connect(self._changed)
        form.addRow("", self.bom_check)
        self._fields["csv_bom"] = self.bom_check
        self.delimiter_combo = QComboBox(page)
        self.delimiter_combo.setEditable(True)
        for text, value in _DELIMITERS:
            self.delimiter_combo.addItem(text, value)
        self.delimiter_combo.setMaximumWidth(120)
        self.delimiter_combo.setToolTip(t("gui.export.delimiter.tip"))
        self.delimiter_combo.currentTextChanged.connect(self._changed)
        form.addRow(t("settings.field.csv_delimiter") + ":", self.delimiter_combo)
        self._fields["csv_delimiter"] = self.delimiter_combo
        self.join_sep_edit = QLineEdit(page)
        self.join_sep_edit.setMaximumWidth(120)
        self.join_sep_edit.setToolTip(t("gui.export.join_sep.tip"))
        self.join_sep_edit.textChanged.connect(self._changed)
        form.addRow(t("settings.field.array_join_sep") + ":", self.join_sep_edit)
        self._fields["array_join_sep"] = self.join_sep_edit
        self._int_row(form, "xlsx_max_rows", 2, XLSX_MAX_ROWS, "gui.export.max_rows.tip")
        return page

    def _view_tab(self) -> QWidget:
        page, form = self._form_page()
        self.theme_combo = QComboBox(page)
        for mode in THEMES:
            self.theme_combo.addItem(t(f"gui.settings.theme.{mode}"), mode)
        self.theme_combo.setToolTip(t("gui.settings.theme.tip"))
        self.theme_combo.currentIndexChanged.connect(self._theme_changed)
        form.addRow(t("gui.settings.theme.label"), self.theme_combo)
        font_row = QHBoxLayout()
        self.mono_default_check = QCheckBox(t("gui.settings.mono.default"), page)
        self.mono_default_check.setToolTip(t("gui.settings.mono.default.tip", family=theme.default_mono_family()))
        self.mono_default_check.toggled.connect(self._mono_default_toggled)
        font_row.addWidget(self.mono_default_check)
        self.mono_font_combo = QFontComboBox(page)
        self.mono_font_combo.setFontFilters(QFontComboBox.FontFilter.MonospacedFonts)
        self.mono_font_combo.setToolTip(t("gui.settings.mono.tip"))
        self.mono_font_combo.currentFontChanged.connect(self._font_changed)
        font_row.addWidget(self.mono_font_combo, 1)
        form.addRow(t("gui.settings.mono.label"), font_row)
        self.mono_size_spin = QSpinBox(page)
        self.mono_size_spin.setRange(0, 48)
        self.mono_size_spin.setSpecialValueText(t("gui.settings.size_default"))
        self.mono_size_spin.setSuffix(" pt")
        self.mono_size_spin.setMaximumWidth(140)
        self.mono_size_spin.valueChanged.connect(self._changed)
        form.addRow(t("gui.settings.mono_size.label"), self.mono_size_spin)
        self.ui_size_spin = QSpinBox(page)
        self.ui_size_spin.setRange(0, 32)
        self.ui_size_spin.setSpecialValueText(t("gui.settings.size_default"))
        self.ui_size_spin.setSuffix(" pt")
        self.ui_size_spin.setMaximumWidth(140)
        self.ui_size_spin.setToolTip(t("gui.settings.ui_size.tip"))
        self.ui_size_spin.valueChanged.connect(self._changed)
        form.addRow(t("gui.settings.ui_size.label"), self.ui_size_spin)
        return page

    def _advanced_tab(self) -> QWidget:
        page, form = self._form_page()
        note = QLabel(t("gui.settings.advanced.note"), page)
        note.setWordWrap(True)
        form.addRow(note)
        self._size_row(form, "large_node_bytes", "gui.settings.large_node_bytes.tip")
        self._int_row(form, "large_node_children", 1, INT_MAX, "gui.settings.large_node_children.tip")
        self._size_row(form, "head_bytes", "gui.settings.head_bytes.tip")
        return page

    def _style_labels(self) -> None:
        self.error_label.setStyleSheet(f"color: {theme.color('danger').name()};")
        self.note_label.setStyleSheet(f"color: {theme.color('warn').name()};")
        for name in self._fields:
            self._mark(name, name in self._errors)

    # ================================================================================
    # values
    # ================================================================================
    def load(self, settings: Settings, *, gui: dict[str, Any] | None = None) -> None:
        """Fill the form from ``settings`` and the GUI state (or ``gui``)."""
        state = self.app.state
        gui = gui if gui is not None else {key: state.get(key) for key in ("theme", "mono_font", "mono_size",
                                                                          "ui_size")}
        self._loading = True
        self._loaded_ints = {name: int(getattr(settings, name)) for name in ("sample_size", "large_node_children")
                             if int(getattr(settings, name)) > INT_MAX}
        self._loaded_gui = dict(gui)
        self._font_edited = False
        try:
            self._fields["sample_size"].setValue(min(INT_MAX, settings.sample_size))
            self.cache_dir_edit.setText(settings.cache_dir or "")
            for name in SIZE_FIELDS:
                self._fields[name].setText(size_text(getattr(settings, name)))
            self._fields["large_node_children"].setValue(min(INT_MAX, settings.large_node_children))
            self._select(self.language_combo, settings.language)
            self._select(self.indent_combo, settings.export_indent,
                         label=t("gui.export.indent.n", n=settings.export_indent))
            self.bom_check.setChecked(bool(settings.csv_bom))
            self._set_delimiter(settings.csv_delimiter)
            self.join_sep_edit.setText(settings.array_join_sep)
            self._fields["xlsx_max_rows"].setValue(settings.xlsx_max_rows)
            mode = gui.get("theme") if gui.get("theme") in THEMES else "system"
            self._select(self.theme_combo, mode)
            family = gui.get("mono_font") or ""
            self.mono_default_check.setChecked(not family)
            self.mono_font_combo.setCurrentFont(QFont(family or theme.default_mono_family()))
            self.mono_font_combo.setEnabled(bool(family))
            self.mono_size_spin.setValue(_int_or_zero(gui.get("mono_size")))
            self.ui_size_spin.setValue(_int_or_zero(gui.get("ui_size")))
        finally:
            self._loading = False
        self._changed()
        self._theme_changed()

    @staticmethod
    def _select(combo: QComboBox, value: Any, label: str | None = None) -> None:
        for i in range(combo.count()):
            data = combo.itemData(i)
            if data == value and type(data) is type(value):
                combo.setCurrentIndex(i)
                return
        if label is not None:
            combo.addItem(label, value)
            combo.setCurrentIndex(combo.count() - 1)

    def _set_delimiter(self, value: str) -> None:
        for i in range(self.delimiter_combo.count()):
            if self.delimiter_combo.itemData(i) == value:
                self.delimiter_combo.setCurrentIndex(i)
                return
        self.delimiter_combo.setEditText(value)

    def _int_value(self, name: str) -> int:
        """A spin box value; a loaded value beyond its range stays until it is edited."""
        if name in self._loaded_ints:
            return self._loaded_ints[name]
        return self._fields[name].value()

    def _int_edited(self, name: str) -> None:
        if not self._loading:
            self._loaded_ints.pop(name, None)
        self._changed()

    def _font_changed(self, *_args: Any) -> None:
        if not self._loading:
            self._font_edited = True
        self._changed()

    def values(self) -> dict[str, Any]:
        """The core settings as typed (sizes as text; validated by :meth:`validate`)."""
        out: dict[str, Any] = {
            "sample_size": self._int_value("sample_size"),
            "cache_dir": self.cache_dir_edit.text().strip() or None,
            "large_node_children": self._int_value("large_node_children"),
            "language": self.language_combo.currentData(),
            "export_indent": self.indent_combo.currentData(),
            "csv_bom": self.bom_check.isChecked(),
            "csv_delimiter": _parse_delimiter(self.delimiter_combo.currentText()),
            "array_join_sep": self.join_sep_edit.text(),
            "xlsx_max_rows": self._fields["xlsx_max_rows"].value(),
        }
        for name in SIZE_FIELDS:
            out[name] = self._fields[name].text()
        return out

    def gui_values(self) -> dict[str, Any]:
        """The GUI preferences: ``theme``, ``mono_font`` (``""`` = default), ``mono_size``
        and ``ui_size`` (0 = default). Values the widgets cannot show exactly (a fractional
        size, a font family that is not installed) stay as loaded until edited."""
        loaded = self._loaded_gui
        if self.mono_default_check.isChecked():
            family = ""
        elif not self._font_edited and loaded.get("mono_font"):
            family = str(loaded["mono_font"])
        else:
            family = self.mono_font_combo.currentFont().family()
        out = {
            "theme": self.theme_combo.currentData() or "system",
            "mono_font": family,
            "mono_size": self.mono_size_spin.value(),
            "ui_size": self.ui_size_spin.value(),
        }
        for key in ("mono_size", "ui_size"):
            old = loaded.get(key)
            if out[key] == _int_or_zero(old) and isinstance(old, (int, float)) and not isinstance(old, bool):
                out[key] = old
        return out

    def _typed_values(self) -> dict[str, Any]:
        """:meth:`values` with the byte sizes read exactly (``Settings`` would read them
        through a float); a size that does not parse stays text and is reported."""
        values = self.values()
        for name in SIZE_FIELDS:
            try:
                parse_size(values[name])          # the accepted syntax and range (finite as a float)
                values[name] = exact_size(values[name])
            except (ValueError, OverflowError):
                pass
        return values

    def settings(self) -> Settings:
        """The edited settings (``UserInputError`` when a value is invalid)."""
        problem = cache_dir_problem(self.cache_dir_edit.text())
        if problem is not None:
            raise UserInputError("settings.invalid", name="cache_dir", label=t("settings.field.cache_dir"),
                                 value=repr(self.cache_dir_edit.text()), expected=problem)
        return Settings.from_dict(self._typed_values(), strict=True)

    def errors(self) -> dict[str, str]:
        """Invalid fields -> localized message (empty when everything is valid)."""
        return dict(self._errors)

    # ================================================================================
    # validation
    # ================================================================================
    def _changed(self, *_args: Any) -> None:
        if self._loading:
            return
        self.validate()

    def validate(self) -> bool:
        """Check every field (``Settings`` validation); mark the invalid ones."""
        errors: dict[str, str] = {}
        values = self._typed_values()
        for name, value in values.items():
            try:
                Settings.from_dict({name: value}, strict=True)
            except UserInputError as exc:
                errors[name] = exc.message
            except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
                errors[name] = str(exc)
        problem = cache_dir_problem(self.cache_dir_edit.text())
        if problem is not None and "cache_dir" not in errors:
            errors["cache_dir"] = problem
        self._errors = errors
        for name in self._fields:
            self._mark(name, name in errors)
        for name in SIZE_FIELDS:
            hint = self._hints[name]
            if name in errors or not isinstance(values[name], int):
                hint.setText("")
            else:
                hint.setText(t("gui.settings.size.exact", bytes=format_count(values[name])))
        self.error_label.setText(copyfmt.escape_surrogates("\n".join(errors.values())))
        self.error_label.setVisible(bool(errors))
        lang = values.get("language")
        notes = []
        if self._env_language is not None:
            notes.append(t("gui.settings.language.env", lang=self._env_language))
        elif lang and lang != self._orig_language:
            notes.append(t("gui.settings.language.restart"))
        self.note_label.setText("\n".join(notes))
        self.note_label.setVisible(bool(notes))
        self.save_button.setEnabled(not errors)
        return not errors

    def _mark(self, name: str, bad: bool) -> None:
        widget = self._fields.get(name)
        if widget is None:
            return
        target = widget.lineEdit() if isinstance(widget, QComboBox) and widget.isEditable() else widget
        if bad:
            target.setStyleSheet(f"border: 1px solid {theme.color('danger').name()};")
            target.setToolTip(self._errors.get(name, ""))
        else:
            target.setStyleSheet("")
        target.setProperty("invalid", bad)

    # ================================================================================
    # live theme, fonts, cache
    # ================================================================================
    def _theme_changed(self, *_args: Any) -> None:
        if self._loading:
            return
        mode = self.theme_combo.currentData() or "system"
        if mode != self._applied_theme:
            self._apply_theme(mode)

    def _apply_theme(self, mode: str) -> None:
        app = QApplication.instance()
        if app is None:  # pragma: no cover - a dialog needs an application
            return
        theme.apply_theme(app, mode)
        self._applied_theme = mode

    def _mono_default_toggled(self, on: bool) -> None:
        self.mono_font_combo.setEnabled(not on)
        self._changed()

    def choose_cache_dir(self) -> None:
        start = self.cache_dir_edit.text().strip() or (self.cache_folder or "")
        path = QFileDialog.getExistingDirectory(self, t("gui.settings.cache_dir.choose"), start)
        if path:
            self.cache_dir_edit.setText(os.path.normpath(path))

    def _measure_cache(self) -> Any:
        settings = get_settings()

        def work(_ctx: Any) -> tuple[str, int]:
            return cache_usage(settings)

        return self.app.tasks.run(work, title=t("gui.settings.task.cache"), kind="cache", owner=self._owner,
                                  silent=True, on_done=self._cache_measured, on_error=self._cache_failed)

    def _cache_measured(self, result: tuple[str, int]) -> None:
        folder, size = result
        self.cache_folder = folder
        self.cache_bytes = size
        self.cache_usage_label.setText(t("gui.settings.cache.usage", size=format_size(size), path=folder))
        self.cache_usage_label.setToolTip(folder)
        self.open_cache_button.setEnabled(True)
        if not get_settings().cache_dir:
            self.cache_dir_edit.setPlaceholderText(t("gui.settings.cache_dir.default_path", path=folder))
        self.cache_measured.emit()

    def _cache_failed(self, exc: BaseException) -> None:
        log.info("could not measure the cache: %s", exc)
        self.cache_usage_label.setText(t("gui.settings.cache.unknown"))
        self.cache_measured.emit()

    def open_cache_folder(self) -> bool:
        return bool(self.cache_folder) and open_local_path(self.cache_folder)

    # ================================================================================
    # buttons
    # ================================================================================
    def restore_defaults(self) -> None:
        """Fill the form with the default values (saved only with "Saqlash")."""
        self.load(Settings(), gui={key: GUI_DEFAULTS.get(key) for key in ("theme", "mono_font", "mono_size",
                                                                         "ui_size")})

    def save(self) -> bool:
        """Validate and save everything (see the module docstring); False when invalid or
        the settings file could not be written (the error is reported)."""
        if not self.validate():
            return False
        try:
            new = self.settings()
            new.save()
        except (JsonReshapeError, OSError) as exc:
            self.app.report_error(exc, self)
            return False
        set_settings(new)
        state = self.app.state
        gui = self.gui_values()
        old_ui = _int_or_zero(state.get("ui_size"))
        for key, value in gui.items():
            state.set(key, value)
        save_state = getattr(self.app, "save_state", None)
        if save_state is not None:
            save_state()                 # in the background (the file lock may be held elsewhere)
        else:  # pragma: no cover - an older context
            state.save()
        if _int_or_zero(gui["ui_size"]) != old_ui:
            _apply_ui_font(_int_or_zero(gui["ui_size"]))
        if self._applied_theme != gui["theme"]:
            self._apply_theme(gui["theme"])
        self._orig_theme = gui["theme"]
        self._orig_language = new.language
        if self._env_language is None and new.language != i18n.get_language():
            i18n.set_language(new.language)     # JSONRESHAPE_LANG, when set, keeps choosing the language
        self.app.settings_changed.emit()
        self.saved.emit()
        return True

    def accept(self) -> None:  # noqa: D401 - Qt name
        if self.save():
            self._done_tasks()
            super().accept()

    def reject(self) -> None:  # noqa: D401 - Qt name
        if self._applied_theme != self._orig_theme:
            self._apply_theme(self._orig_theme)
        self._done_tasks()
        super().reject()

    def _done_tasks(self) -> None:
        try:
            self.app.tasks.detach(self._owner)
        except Exception:  # pragma: no cover - defensive
            log.debug("detach failed", exc_info=True)


def _int_or_zero(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    return 0


def _apply_ui_font(size: int) -> None:
    """Set the application font size (0: the system UI font)."""
    app = QApplication.instance()
    if app is None:  # pragma: no cover
        return
    if size > 0:
        font = QFont(QApplication.font())
        font.setPointSizeF(float(size))
    else:
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    QApplication.setFont(font)

