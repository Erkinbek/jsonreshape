"""Import dialog (docs/ARCHITECTURE.md §17; core: :mod:`jsonreshape.importers`): File ▸ "Import (CSV, YAML, XML)…".

:class:`ImportDialog` (non-modal) converts a CSV / TSV, YAML or XML file into a JSON / JSON Lines file with
:func:`jsonreshape.importers.import_file` and offers to open the result in a new tab:

* **source**: any file ("Tanlash…" with filters per type; the injected ``ask_open_path``); the type detected from
  the extension is shown (``detected_label``) and can be overridden with the format combo ("Kengaytma bo'yicha" /
  CSV / YAML / XML); the option boxes of the other types are hidden;
* **options**: values (``types`` auto / string, the empty-cell rule), the CSV box (delimiter: automatic / ``,`` /
  ``;`` / tab / ``|``; header row; nested ``a.b.c`` columns; encoding: automatic or a codec name; skip malformed
  rows), the XML box (record tag or record path) and the JSON indentation of a ``.json`` target (2 / 4 / tab /
  compact; JSON Lines is always compact);
* **target**: default ``<stem>.jsonl`` beside the source (``<stem>.json`` for YAML: one document gives one JSON
  value), following the source and the chosen type until the user edits it; "Tanlash…" (``ask_save_path``). The
  output format follows the target's extension (``.json`` = one array / value, anything else JSON Lines). The target
  is NEVER the source -- refused in any spelling or through a link (:func:`~jsonreshape.gui.searchpanel.check_not_source`
  and once more by :func:`import_file`); an existing target is replaced after a confirmation; a target that another
  tab shows is refused while that tab has unsaved, pending or running changes (or another window of the program
  edits the file) and asked about when the tab shows it unchanged -- that tab opens the new file once it was written
  (review S3, :func:`~jsonreshape.gui.searchpanel.open_target_check`);
* **Import** runs as ONE heavy task (kind ``import``) with progress (bytes of the source, rate, time left;
  "Navbatda" while it waits for a heavy-task slot) and "To'xtatish" (cancel: nothing is written);
* the **report**: the headline (records, size, time), the notes (converted numbers, literals, empty cells, YAML
  dates / binaries / by-value numbers) and the warnings (skipped rows, big YAML, non-finite values, no records);
  "Natijani ochish" calls ``open_result(target, options)`` (default ``app.request_open``: a new tab; the options
  are ``{"format": "jsonl"}`` for JSON Lines, ``{}`` for JSON -- the reader detects the kind).

Closing the dialog while an import runs asks first and stops it. The task of a run belongs to a small job object
(not to the dialog): a result arriving after the dialog closed is dropped.

Integration API: ``ImportDialog(app, source=None, parent=None, *, target=None, open_result=None,
ask_open_path=None, ask_save_path=None)`` and ``show_import_dialog(app, source=None, parent=None, **kw)`` (one
window; brought to the front when open). The path askers are ``fn(parent, title, start_path, filters) -> str | None``.
"""

from __future__ import annotations

import logging
import os
import weakref
from pathlib import Path
from typing import Any, Callable

import shiboken6
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from jsonreshape.errors import JsonReshapeError, UserInputError
from jsonreshape.fileio import long_path
from jsonreshape.gui import copyfmt, theme
from jsonreshape.gui.searchpanel import check_not_source, open_target_check, reload_tabs
from jsonreshape.gui.widgets import confirm, format_count, keep_alive
from jsonreshape.i18n import t
from jsonreshape.importers import (
    EMPTY_MODES,
    FORMATS,
    TYPE_MODES,
    ImportOptions,
    detect_format,
    format_name,
    import_file,
    output_format_for,
)
from jsonreshape.tasks import format_bytes, format_duration

log = logging.getLogger(__name__)

__all__ = ["DELIMITERS", "ENCODINGS", "INDENTS", "ImportDialog", "default_target", "show_import_dialog"]

PathAsker = Callable[[Any, str, str, str], "str | None"]   # (parent, title, start path, filters) -> path

#: delimiter combo: (i18n key suffix, value; None = sniffed)
DELIMITERS: tuple[tuple[str, str | None], ...] = (("auto", None), ("comma", ","), ("semicolon", ";"),
                                                  ("tab", "\t"), ("pipe", "|"))
#: encoding combo suggestions (editable; "" = detected)
ENCODINGS = ("", "utf-8", "utf-16", "cp1251", "cp1252", "latin-1", "koi8-r", "cp866")
#: indent combo: (i18n key suffix, ImportOptions.indent)
INDENTS: tuple[tuple[str, int | str | None], ...] = (("2", 2), ("4", 4), ("tab", "\t"), ("compact", None))

_OPEN: list[weakref.ReferenceType[Any]] = []    # the open dialogs (show_import_dialog reuses one)


# ==================================================================================
# helpers
# ==================================================================================
def ask_save_file(parent: Any, title: str, start: str, filters: str) -> str | None:
    path, _selected = QFileDialog.getSaveFileName(parent, title, start, filters)
    return path or None


def ask_open_file(parent: Any, title: str, start: str, filters: str) -> str | None:
    path, _selected = QFileDialog.getOpenFileName(parent, title, start, filters)
    return path or None


def _alive(obj: Any) -> bool:
    try:
        return obj is not None and bool(shiboken6.isValid(obj))
    except Exception:  # pragma: no cover - shiboken always available with PySide6
        return False


def _error_text(exc: BaseException | None) -> str:
    if exc is None:
        return ""
    if isinstance(exc, JsonReshapeError):
        hint = exc.hint
        return exc.message + (" " + hint if hint else "")
    return str(exc) or type(exc).__name__


def _status(app: Any, text: str) -> None:
    show = getattr(app, "show_status", None)
    if callable(show):
        show(text, 6000)


def _clean_path(text: str) -> str:
    s = text.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    return s


def default_target(source: str | os.PathLike[str], fmt: str | None = None) -> str:
    """``<stem>.jsonl`` beside ``source`` (``<stem>.json`` for a YAML source: one document gives one JSON value);
    ``fmt`` overrides the type detected from the extension."""
    p = Path(os.fspath(source))
    kind = fmt or detect_format(p)
    stem = p.stem or p.name
    return str(p.with_name(f"{stem}{'.json' if kind == 'yaml' else '.jsonl'}"))


class _ImportJob:
    """One import run: the task's owner and callbacks (plain Python). A result the dialog does not take any more
    (closed, superseded, destroyed) is dropped."""

    def __init__(self, dialog: ImportDialog, reload: list | None = None) -> None:
        self._dialog = weakref.ref(dialog)
        self.reload = list(reload or ())     # weak refs: tabs that showed the target (reopened once it is written)
        self.task: Any = None
        self.state = "running"

    def dialog(self) -> ImportDialog | None:
        d = self._dialog()
        if d is None or not _alive(d) or d._job is not self:
            return None
        return d

    def done(self, result: Any) -> None:
        self.state = "done"
        reload, self.reload = self.reload, []
        reload_tabs(reload)                   # also when nobody waits for the report any more
        d = self.dialog()
        if d is not None:
            d._job_done(self, result)

    def failed(self, exc: BaseException) -> None:
        self.state = "failed"
        d = self.dialog()
        if d is not None:
            d._job_failed(self, exc)

    def cancelled(self) -> None:
        self.state = "cancelled"
        d = self.dialog()
        if d is not None:
            d._job_cancelled(self)


# ==================================================================================
# the dialog
# ==================================================================================
class ImportDialog(QDialog):
    """Convert a CSV / YAML / XML file into a new JSON / JSON Lines file (module docstring).

    Signals: ``run_started(task)``, ``run_finished(report)``, ``run_failed(exc)``, ``run_cancelled()``."""

    run_started = Signal(object)
    run_finished = Signal(object)
    run_failed = Signal(object)
    run_cancelled = Signal()

    def __init__(self, app: Any, source: str | os.PathLike[str] | None = None, parent: QWidget | None = None, *,
                 target: str | os.PathLike[str] | None = None, open_result: Callable[[str, dict], Any] | None = None,
                 ask_open_path: PathAsker | None = None, ask_save_path: PathAsker | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self._open_cb = open_result
        self._ask_open = ask_open_path or ask_open_file
        self._ask_save = ask_save_path or ask_save_file
        self._job: _ImportJob | None = None
        self._report: Any = None
        self._closing = False
        self._target_auto = True
        self._queued_shown = False
        self.setObjectName("import_dialog")
        self.setSizeGripEnabled(True)
        self._build()
        if source:
            self.set_source_path(source)
        if target:
            self.set_target_path(target)
        self._update_title()
        app.tasks.task_updated.connect(self._on_task_updated)
        self._update_type()
        self._refresh()
        self.resize(760, 620)

    # ------------------------------------------------------------------ construction
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        intro = QLabel(t("gui.importdialog.intro"), self)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        files = QFormLayout()
        self.source_edit = QLineEdit(self)
        self.source_edit.setObjectName("source_edit")
        self.source_edit.setClearButtonEnabled(True)
        self.source_edit.textChanged.connect(self._on_source_changed)
        self.source_button = QPushButton(t("gui.importdialog.browse"), self)
        self.source_button.setAutoDefault(False)
        self.source_button.clicked.connect(self.choose_source)
        row = QHBoxLayout()
        row.addWidget(self.source_edit, 1)
        row.addWidget(self.source_button)
        source_label = QLabel(t("gui.importdialog.source.label"), self)
        source_label.setBuddy(self.source_edit)
        files.addRow(source_label, row)

        self.format_combo = QComboBox(self)
        self.format_combo.setObjectName("format_combo")
        self.format_combo.addItem(t("gui.importdialog.format.auto"), "")
        for fmt in FORMATS:
            self.format_combo.addItem(format_name(fmt), fmt)
        self.format_combo.currentIndexChanged.connect(lambda _i: self._update_type())
        self.detected_label = self._label("detected_label")
        row = QHBoxLayout()
        row.addWidget(self.format_combo)
        row.addWidget(self.detected_label, 1)
        format_label = QLabel(t("gui.importdialog.format.label"), self)
        format_label.setBuddy(self.format_combo)
        files.addRow(format_label, row)

        self.target_edit = QLineEdit(self)
        self.target_edit.setObjectName("target_edit")
        self.target_edit.setClearButtonEnabled(True)
        self.target_edit.textEdited.connect(self._on_target_edited)
        self.target_edit.textChanged.connect(lambda _text: self._refresh())
        self.target_button = QPushButton(t("gui.importdialog.browse"), self)
        self.target_button.setAutoDefault(False)
        self.target_button.clicked.connect(self.choose_target)
        row = QHBoxLayout()
        row.addWidget(self.target_edit, 1)
        row.addWidget(self.target_button)
        target_label = QLabel(t("gui.importdialog.target.label"), self)
        target_label.setBuddy(self.target_edit)
        files.addRow(target_label, row)
        layout.addLayout(files)

        # -- options ---------------------------------------------------------------------------
        self.options_box = QGroupBox(t("gui.importdialog.options"), self)
        form = QFormLayout(self.options_box)
        self.types_combo = QComboBox(self.options_box)
        for mode in TYPE_MODES:
            self.types_combo.addItem(t(f"gui.importdialog.types.{mode}"), mode)
        self.types_combo.setToolTip(t("gui.importdialog.types.tip"))
        types_label = QLabel(t("gui.importdialog.types.label"), self.options_box)
        types_label.setBuddy(self.types_combo)
        form.addRow(types_label, self.types_combo)
        self.indent_combo = QComboBox(self.options_box)
        for key, value in INDENTS:
            self.indent_combo.addItem(t(f"gui.importdialog.indent.{key}"), value)
        self.indent_combo.setToolTip(t("gui.importdialog.indent.tip"))
        self.indent_label = QLabel(t("gui.importdialog.indent.label"), self.options_box)
        self.indent_label.setBuddy(self.indent_combo)
        form.addRow(self.indent_label, self.indent_combo)

        self.csv_box = QGroupBox(t("gui.importdialog.csv"), self.options_box)
        csv_form = QFormLayout(self.csv_box)
        self.delimiter_combo = QComboBox(self.csv_box)
        for key, value in DELIMITERS:
            self.delimiter_combo.addItem(t(f"gui.importdialog.delimiter.{key}"), value)
        delimiter_label = QLabel(t("gui.importdialog.delimiter.label"), self.csv_box)
        delimiter_label.setBuddy(self.delimiter_combo)
        csv_form.addRow(delimiter_label, self.delimiter_combo)
        self.encoding_combo = QComboBox(self.csv_box)
        self.encoding_combo.setEditable(True)
        self.encoding_combo.addItem(t("gui.importdialog.encoding.auto"), "")
        for enc in ENCODINGS[1:]:
            self.encoding_combo.addItem(enc, enc)
        self.encoding_combo.setToolTip(t("gui.importdialog.encoding.tip"))
        encoding_label = QLabel(t("gui.importdialog.encoding.label"), self.csv_box)
        encoding_label.setBuddy(self.encoding_combo)
        csv_form.addRow(encoding_label, self.encoding_combo)
        self.empty_combo = QComboBox(self.csv_box)
        for mode in EMPTY_MODES:
            self.empty_combo.addItem(t(f"gui.importdialog.empty.{mode}"), mode)
        empty_label = QLabel(t("gui.importdialog.empty.label"), self.csv_box)
        empty_label.setBuddy(self.empty_combo)
        csv_form.addRow(empty_label, self.empty_combo)
        self.header_check = QCheckBox(t("gui.importdialog.header"), self.csv_box)
        self.header_check.setChecked(True)
        self.header_check.setToolTip(t("gui.importdialog.header.tip"))
        csv_form.addRow(self.header_check)
        self.nest_check = QCheckBox(t("gui.importdialog.nest"), self.csv_box)
        self.nest_check.setChecked(True)
        self.nest_check.setToolTip(t("gui.importdialog.nest.tip"))
        csv_form.addRow(self.nest_check)
        self.skip_bad_check = QCheckBox(t("gui.importdialog.skip_bad"), self.csv_box)
        self.skip_bad_check.setToolTip(t("gui.importdialog.skip_bad.tip"))
        csv_form.addRow(self.skip_bad_check)
        form.addRow(self.csv_box)

        self.xml_box = QGroupBox(t("gui.importdialog.xml"), self.options_box)
        xml_form = QFormLayout(self.xml_box)
        self.record_tag_edit = QLineEdit(self.xml_box)
        self.record_tag_edit.setPlaceholderText(t("gui.importdialog.record_tag.placeholder"))
        self.record_tag_edit.setClearButtonEnabled(True)
        tag_label = QLabel(t("gui.importdialog.record_tag.label"), self.xml_box)
        tag_label.setBuddy(self.record_tag_edit)
        xml_form.addRow(tag_label, self.record_tag_edit)
        self.record_path_edit = QLineEdit(self.xml_box)
        self.record_path_edit.setPlaceholderText(t("gui.importdialog.record_path.placeholder"))
        self.record_path_edit.setClearButtonEnabled(True)
        path_label = QLabel(t("gui.importdialog.record_path.label"), self.xml_box)
        path_label.setBuddy(self.record_path_edit)
        xml_form.addRow(path_label, self.record_path_edit)
        xml_note = QLabel(t("gui.importdialog.xml.note"), self.xml_box)
        xml_note.setWordWrap(True)
        theme.bind_style(xml_note, "color: $muted;")
        xml_form.addRow(xml_note)
        form.addRow(self.xml_box)
        layout.addWidget(self.options_box)

        # -- run ----------------------------------------------------------------------------------
        run = QHBoxLayout()
        self.run_button = QPushButton(t("gui.importdialog.run"), self)
        self.run_button.setToolTip(t("gui.importdialog.run.tip"))
        self.run_button.setDefault(True)
        self.run_button.clicked.connect(lambda: self.run_import())
        run.addWidget(self.run_button)
        self.stop_button = QPushButton(t("gui.importdialog.stop"), self)
        self.stop_button.setAutoDefault(False)
        self.stop_button.clicked.connect(self.cancel_run)
        self.stop_button.setVisible(False)
        run.addWidget(self.stop_button)
        run.addStretch(1)
        layout.addLayout(run)

        self.progress_bar = QProgressBar(self)
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(8)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        self.status_label = self._label("status_label", selectable=True)
        layout.addWidget(self.status_label)
        self.error_label = self._label("error_label")
        self.error_label.setVisible(False)
        theme.bind_style(self.error_label, "color: $danger;")
        layout.addWidget(self.error_label)

        # -- the report ---------------------------------------------------------------------------
        self.report_box = QGroupBox(t("gui.importdialog.report"), self)
        self.report_box.setObjectName("report_box")
        rl = QVBoxLayout(self.report_box)
        self.headline_label = self._label("headline_label", selectable=True, bold=True)
        rl.addWidget(self.headline_label)
        self.notes_label = self._label("notes_label", selectable=True)
        theme.bind_style(self.notes_label, "color: $muted;")
        rl.addWidget(self.notes_label)
        self.warnings_label = self._label("warnings_label", selectable=True)
        theme.bind_style(self.warnings_label, "color: $warn;")
        rl.addWidget(self.warnings_label)
        buttons = QHBoxLayout()
        self.open_button = QPushButton(t("gui.importdialog.open"), self.report_box)
        self.open_button.setToolTip(t("gui.importdialog.open.tip"))
        self.open_button.setAutoDefault(False)
        self.open_button.clicked.connect(lambda: self.open_result())
        buttons.addWidget(self.open_button)
        buttons.addStretch(1)
        rl.addLayout(buttons)
        self.report_box.setVisible(False)
        layout.addWidget(self.report_box)
        layout.addStretch(1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        self.close_button = QPushButton(t("gui.common.close"), self)
        self.close_button.setAutoDefault(False)
        self.close_button.clicked.connect(self.close)
        close_row.addWidget(self.close_button)
        layout.addLayout(close_row)

    def _label(self, name: str, *, selectable: bool = False, bold: bool = False) -> QLabel:
        label = QLabel(self)
        label.setObjectName(name)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        if selectable:
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if bold:
            font = label.font()
            font.setBold(True)
            label.setFont(font)
        return label

    def _update_title(self) -> None:
        src = self.source_path()
        if src:
            self.setWindowTitle(t("gui.importdialog.title", name=os.path.basename(src)))
        else:
            self.setWindowTitle(t("gui.importdialog.title_none"))

    # ------------------------------------------------------------------ inputs
    def source_path(self) -> str:
        return _clean_path(self.source_edit.text())

    def set_source_path(self, path: str | os.PathLike[str]) -> None:
        """The file to import (the target follows it until the user chose a target)."""
        self.source_edit.setText(os.fspath(path))

    def target_path(self) -> str:
        return _clean_path(self.target_edit.text())

    def set_target_path(self, path: str | os.PathLike[str]) -> None:
        self._target_auto = False
        self.target_edit.setText(os.fspath(path))

    def detected_format(self) -> str | None:
        """The type by the source's extension (``csv`` | ``yaml`` | ``xml`` | ``None``)."""
        src = self.source_path()
        return detect_format(src) if src else None

    def current_format(self) -> str | None:
        """The type that will be imported: the combo's choice, else the detected one."""
        chosen = self.format_combo.currentData()
        return chosen or self.detected_format()

    def output_format(self) -> str:
        """``json`` when the target ends in ``.json``, else ``jsonl``."""
        return output_format_for(self.target_path()) or "jsonl"

    def _update_type(self) -> None:
        if not _alive(self):
            return
        detected = self.detected_format()
        fmt = self.current_format()
        if not self.source_path():
            self.detected_label.setText("")
        elif detected is None:
            self.detected_label.setText(t("gui.importdialog.detected.none"))
        else:
            self.detected_label.setText(t("gui.importdialog.detected", fmt=format_name(detected)))
        self.csv_box.setVisible(fmt in (None, "csv"))
        self.xml_box.setVisible(fmt == "xml")
        if self._target_auto:
            src = self.source_path()
            self.target_edit.setText(default_target(src, fmt) if src else "")
        self._refresh()

    def _on_source_changed(self, _text: str) -> None:
        self._update_title()
        self._update_type()

    def _on_target_edited(self, _text: str) -> None:
        self._target_auto = False

    def choose_source(self) -> None:
        """"Tanlash…" next to the source."""
        path = self._ask_open(self, t("gui.importdialog.source.title"), self.source_path(),
                              t("gui.importdialog.source.filter"))
        if path:
            self.set_source_path(path)

    def choose_target(self) -> None:
        """"Tanlash…" next to the target."""
        src = self.source_path()
        start = self.target_path() or (default_target(src, self.current_format()) if src else "")
        path = self._ask_save(self, t("gui.importdialog.target.title"), start, t("gui.importdialog.target.filter"))
        if path:
            self.set_target_path(path)

    def options(self) -> ImportOptions:
        """The :class:`ImportOptions` of the widgets (raises ``UserInputError`` for a bad encoding)."""
        fmt = self.format_combo.currentData() or None
        encoding = self.encoding_combo.currentData() if self.encoding_combo.currentIndex() >= 0 and \
            self.encoding_combo.currentText() == self.encoding_combo.itemText(self.encoding_combo.currentIndex()) \
            else self.encoding_combo.currentText().strip()
        return ImportOptions(fmt=fmt, output=self.output_format(), indent=self.indent_combo.currentData(),
                             types=self.types_combo.currentData(), empty=self.empty_combo.currentData(),
                             delimiter=self.delimiter_combo.currentData(), header=self.header_check.isChecked(),
                             nest=self.nest_check.isChecked(), encoding=encoding or None,
                             skip_bad=self.skip_bad_check.isChecked(),
                             record_tag=self.record_tag_edit.text().strip() or None,
                             record_path=self.record_path_edit.text().strip() or None)

    # ------------------------------------------------------------------ state
    def _set_error(self, text: str | None) -> None:
        self.error_label.setText(text or "")
        self.error_label.setVisible(bool(text))

    def error_text(self) -> str:
        return self.error_label.text()

    def status_text(self) -> str:
        return self.status_label.text()

    def headline_text(self) -> str:
        return self.headline_label.text()

    @property
    def running(self) -> bool:
        return self._job is not None and self._job.state == "running"

    @property
    def task(self) -> Any:
        return self._job.task if self._job is not None else None

    @property
    def report(self) -> Any:
        """The :class:`~jsonreshape.importers.ImportReport` of the last finished run (``None`` before)."""
        return self._report

    # ------------------------------------------------------------------ running
    def run_import(self) -> Any:
        """"Import": start the conversion; returns the task (``None`` when nothing started: the error is shown)."""
        if self.running or self._closing:
            return None
        src_text = self.source_path()
        if not src_text:
            self._set_error(t("gui.importdialog.source.need"))
            return None
        src = os.path.abspath(src_text)
        tgt_text = self.target_path()
        if not tgt_text:
            self._set_error(t("gui.importdialog.target.need"))
            return None
        tgt = os.path.abspath(tgt_text)
        try:
            opts = self.options()
            if not os.path.exists(long_path(src)):
                raise UserInputError("error.file_not_found", path=src)
            if opts.fmt is None and detect_format(src) is None:
                raise UserInputError("importers.error.unknown_format", path=src, extensions=".csv .tsv .yaml .yml .xml")
            check_not_source(tgt, (src,))
            if os.path.isdir(long_path(tgt)):
                raise UserInputError("fileio.error.is_directory", path=tgt)
        except JsonReshapeError as exc:
            text = exc.message
            if getattr(exc, "key", "") == "writer.error.same_file":
                text += " " + t("gui.importdialog.same_file.hint")
            elif getattr(exc, "key", "") == "importers.error.unknown_format":
                text += " " + t("gui.importdialog.unknown_format.hint")
            self._set_error(text)
            return None
        self._set_error(None)
        found = open_target_check(self, tgt, on_refused=self._set_error)
        if found is None:
            return None
        if not found and os.path.exists(long_path(tgt)) and not confirm(self, t("gui.importdialog.overwrite", path=tgt)):
            return None
        self._report = None
        self.report_box.setVisible(False)
        settings = getattr(self.app, "settings", None)
        job = _ImportJob(self, reload=found)

        def work(ctx: Any) -> Any:
            return import_file(src, tgt, opts, settings=settings, progress=ctx.progress, cancel=ctx.cancel)

        title = t("gui.importdialog.task", name=os.path.basename(src))
        job.task = self.app.tasks.run(work, title=title, kind="import", owner=job, heavy=True, on_done=job.done,
                                      on_error=job.failed, on_cancel=job.cancelled)
        self._job = job
        self._queued_shown = False
        self.status_label.setText(title)
        self._refresh()
        self.run_started.emit(job.task)
        return job.task

    def cancel_run(self) -> None:
        """Stop the running import (nothing is written)."""
        if self.running:
            self._job.task.cancel()     # type: ignore[union-attr]

    def _job_done(self, job: _ImportJob, report: Any) -> None:
        self._job = None
        self._report = report
        self.status_label.setText("")
        self._fill_report(report)
        _status(self.app, t("gui.importdialog.status.written", path=str(report.output)))
        self._refresh()
        self.run_finished.emit(report)

    def _job_failed(self, job: _ImportJob, exc: BaseException) -> None:
        self._job = None
        self.status_label.setText("")
        self._set_error(t("gui.importdialog.failed", error=_error_text(exc)))
        if not isinstance(exc, JsonReshapeError):
            self.app.report_error(exc, self)
        self._refresh()
        self.run_failed.emit(exc)

    def _job_cancelled(self, job: _ImportJob) -> None:
        self._job = None
        self.status_label.setText(t("gui.importdialog.cancelled"))
        self._refresh()
        self.run_cancelled.emit()

    def _on_task_updated(self, task: Any) -> None:
        job = self._job
        if job is None or task is None or task is not job.task or task.state != "running" or self._closing:
            return
        if getattr(task, "queued", False):
            self._queued_shown = True
            self.progress_bar.setRange(0, 0)
            self.status_label.setText(t("gui.tasks.queued"))
            return
        info = task.info()
        if info is None:
            self.progress_bar.setRange(0, 0)
            return
        if info.fraction is None:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(int(max(0.0, min(1.0, info.fraction)) * 1000))
        parts = [info.message] if info.message else []
        if info.total:
            values = {"done": format_bytes(info.done), "total": format_bytes(info.total),
                      "rate": format_bytes(info.rate)}
            if info.eta is not None and info.eta > 0:
                parts.append(t("gui.importdialog.progress_eta", eta=format_duration(info.eta), **values))
            else:
                parts.append(t("gui.importdialog.progress", **values))
        if parts:
            self.status_label.setText(copyfmt.escape_surrogates(" — ".join(parts)))

    # ------------------------------------------------------------------ the report
    def _fill_report(self, rep: Any) -> None:
        self.headline_label.setText(t("gui.importdialog.report.summary", records=format_count(rep.records),
                                      source=os.path.basename(str(rep.source)), fmt=format_name(rep.fmt),
                                      output=os.path.basename(str(rep.output)),
                                      kind=t(f"gui.importdialog.output.{rep.output_fmt}"),
                                      size=format_bytes(rep.bytes_written), seconds=format_duration(rep.elapsed)))
        self.notes_label.setText("\n".join(rep.notes))
        self.notes_label.setVisible(bool(rep.notes))
        self.warnings_label.setText("\n".join(rep.warnings))
        self.warnings_label.setVisible(bool(rep.warnings))
        self.report_box.setVisible(True)

    def open_result(self) -> bool:
        """"Natijani ochish": ``open_result(target, options)`` (default: ``app.request_open``, a new tab)."""
        rep = self._report
        if rep is None or self.running:
            return False
        path = str(rep.output)
        options: dict[str, Any] = {"format": "jsonl"} if rep.output_fmt == "jsonl" else {}
        if self._open_cb is not None:
            self._open_cb(path, options)
            return True
        request = getattr(self.app, "request_open", None)
        if not callable(request):
            return False
        request(path, options)
        return True

    # ------------------------------------------------------------------ enabling
    def _refresh(self) -> None:
        if not _alive(self):
            return
        running = self.running
        self.run_button.setEnabled(bool(self.source_path()) and bool(self.target_path()) and not running
                                   and not self._closing)
        self.stop_button.setVisible(running)
        self.progress_bar.setVisible(running)
        if running and not self._queued_shown:
            self.progress_bar.setRange(0, 0)
        for widget in (self.options_box, self.source_edit, self.source_button, self.target_edit, self.target_button,
                       self.format_combo):
            widget.setEnabled(not running)
        is_json = self.output_format() == "json"
        self.indent_combo.setEnabled(is_json)
        self.indent_label.setEnabled(is_json)
        self.open_button.setEnabled(self._report is not None and not running)

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt name
        self._closing = False
        super().showEvent(event)
        self._refresh()

    def done(self, result: int) -> None:  # noqa: D401 - Qt name
        """Close the window: a running import is stopped after a confirmation (declined: the window stays open)."""
        if not self._closing and self.running:
            if not confirm(self, t("gui.importdialog.confirm_stop")):
                return
            self.cancel_run()
        self._closing = True
        self._job = None              # a late result is dropped
        super().done(result)


def show_import_dialog(app: Any, source: str | os.PathLike[str] | None = None, parent: QWidget | None = None,
                       **kwargs: Any) -> ImportDialog:
    """File ▸ "Import (CSV, YAML, XML)…": the import window, shown non-modal and deleted when closed; an open one
    is brought to the front (and gets ``source`` when given)."""
    for ref in list(_OPEN):
        dialog = ref()
        if dialog is None or not _alive(dialog) or not dialog.isVisible() or dialog._closing:
            _OPEN.remove(ref)
            continue
        if source and not dialog.running:
            dialog.set_source_path(source)
        dialog.raise_()
        dialog.activateWindow()
        return dialog
    dialog = ImportDialog(app, source, parent, **kwargs)
    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    if parent is None:
        keep_alive(dialog)
    _OPEN.append(weakref.ref(dialog))
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
