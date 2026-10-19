"""The main window (docs/ARCHITECTURE.md §11.8; AGENT_PROMPT §4.6, §5, §11).

:class:`MainWindow` holds the document tabs (:class:`~jsonreshape.gui.doctab.DocumentTab`,
closable, movable; the title is the file name plus a busy marker while it is opened /
indexed, the tooltip the full path), the menus (Fayl, Tahrir, Ko'rinish, Navigatsiya,
Qidiruv, Asboblar, Yordam), a toolbar with the most used commands, the docks (search,
filter, statistics, tasks; object names ``dock_search`` / ``dock_filter`` / ``dock_stats`` /
``dock_tasks`` so ``saveState()`` / ``restoreState()`` keep their places) and the
:class:`~jsonreshape.gui.statusbar.StatusBar`.

Commands that act on a view (copy, zoom, quick find, F3, expand ...) go to the *active
view* of the current tab (:meth:`DocumentTab.active_view`); their shortcuts are bound to
the document area (``WidgetWithChildrenShortcut``), so the docks keep their own keys.
Actions are enabled by context: no file open, the document still opening, a single-value
document (no filter / sort), the history position, the active view's capabilities.

Views and panels are injected (tests use stubs): ``view_registry`` (see
:func:`~jsonreshape.gui.doctab.default_view_registry`) and ``panel_factories`` --
``search`` / ``filter`` ``(app, parent)``, ``sort`` / ``export`` ``(session, parent, ...)``,
``settings`` ``(app, parent)`` -- by default imported lazily from the real modules
(:func:`default_panel_factories`); a panel that cannot be created is logged and its
commands are disabled.

Opening: :meth:`MainWindow.open_path` (one :class:`~jsonreshape.gui.session.DocSession`
per tab; ``options``: ``record_path``, ``format``, ``ref`` -- a node to reveal once the
document is open), the open dialog (Ctrl+O; the last folder is remembered), the recent
files (a missing file is removed from the list when opening it fails), drag & drop of
files, ``AppContext.open_requested`` ("open in a new tab") and the command line. A file
already open gets another tab (two views of one file are allowed) unless it was chosen in
the open dialog, the recent files or dropped without options -- then its tab is focused.

The window geometry and dock/toolbar state are saved in the GUI state when the window
closes and restored when it is created. Other GUI state changes (recent files, theme) are
saved by a silent background task (``AppContext.save_state``): the state file's
inter-process lock never blocks the GUI thread.

Lifetime: the window connects to sessions and tabs only with bound methods (the tab of a
session signal is found through ``sender()``), and closing a tab deletes the tab and its
session, so closed tabs are freed. An export of the closed tab's file goes on (the status
bar says so; its report still appears); closing the window with exports still running
asks first.

Dropped folders are ignored (status message); :meth:`MainWindow.open_command_line` opens
the command-line files (empty arguments and folders ignored, a file named twice opened
once). Saving new settings re-applies the UI font size (the views follow
``settings_changed`` for their data font).

Editing (docs/ARCHITECTURE.md §12.9): **File** ▸ Saqlash (Ctrl+S) / Boshqacha saqlash
(Ctrl+Shift+S: a save dialog offering the document's own format and compression; a name of
another format -- ``.csv``, ``.jsonl`` for a JSON array, ... -- offers the export dialog
instead, since Save As keeps the format); **Tahrir** ▸ Ortga qaytarish (Ctrl+Z) / Qayta
bajarish (Ctrl+Y, Ctrl+Shift+Z) with the step labels, the view edit commands of
:data:`~jsonreshape.gui.actions.EDIT_COMMANDS` (their keys are shown for display only --
Enter, F2, Del, Ins, Ctrl+D, Ctrl+V, Alt+↑/↓ belong to the views' grids and are never
window or document-area shortcuts), the copy commands, Ommaviy amallar… (Ctrl+H, the
session's :class:`~jsonreshape.gui.bulkdialog.BulkDialog`, one per document), JSON Patch
import / export, Barcha o'zgarishlarni tashlab yuborish (the controller asks); **Navigatsiya**
▸ Xatcho'p qo'shish is Ctrl+B (Ctrl+D duplicates nodes in the views). Everything acts
through the current tab's edit controller (``session.edit``) and is enabled from its state
(``available``, ``busy``, ``dirty``, ``can_undo`` / ``can_redo``); the tab and window titles
start with ``*`` while there are unsaved changes.

Structure builder (docs/ARCHITECTURE.md §13.5): **Ko'rinish** ▸ Tuzilma quruvchi (Ctrl+6, from
``VIEW_KINDS``). A view with an undo history of its own (``undo()`` / ``redo()`` and
``can_undo`` / ``can_redo`` / ``undo_text``: the structure builder) takes the window's Ctrl+Z /
Ctrl+Y / Ctrl+Shift+Z and the Edit menu's undo / redo while it is the active view -- the
document's edit undo is not reachable then; its tab's ``view_state_changed`` refreshes them.
Ctrl+E (export) asks the active view's ``export_with_mapping()`` first when it has one (the
export dialog in mapping mode).

Closing: :meth:`MainWindow.close_tab` returns ``True`` when the tab was closed now; a tab
with unsaved changes asks first (non-blocking; it closes when answered). Closing the window
ignores the close event while dirty tabs ask one by one (Save / Don't save / Cancel), then
closes again (a Cancel keeps the window open); headless without an answer handler the edit
layers are kept for recovery. After the event loop ended (:func:`~jsonreshape.gui.app.main`)
:meth:`MainWindow.close_without_questions` closes at once, keeping the layers.

Fix round 3 (review3):

* **What is being typed is not lost** (M3): Save, Save As, closing a tab, closing the window and
  F5 first commit the tab's open inline editors (``editors.InlineEditor.commit()``, found as
  children of the tab -- no view method needed) and the structure builder's rename
  (``finish_rename()``), and go on once the editors' steps were applied
  (``EditController.when_idle``). An editor with an invalid value (or a key check still on its
  way, or a busy controller) refuses the command with a status message and stays open.
* **A busy but unchanged document asks too** (CL-4): a tab whose controller runs a long
  operation (``needs_close_answer``: Save As, patch import, ...) is asked "wait / stop / cancel"
  before it closes, reopens (F5) or the window quits -- the tab's ``confirm_close`` asks only
  documents with unsaved changes, so the window asks the controller itself then.
* The Edit menu shows each view's own keys (``actions.command_keys``) and disables the "change
  type" items that §12.4 refuses for the selected nodes (``actions.change_type_refusal``).
* Export has its own toolbar icon (a page with an arrow, drawn in the theme's colors and redrawn
  when the theme changes; Save keeps the disk); error windows opened without a parent close with
  the window (they would keep the application running: L9).

Tools (stage 5, docs/ARCHITECTURE.md §14.9): **Asboblar** ▸ Ikki faylni taqqoslash… (always),
Maydon statistikasi… (the statistics dock ``stats`` of :data:`DOCK_PANELS`, also in View ▸ Panels:
:meth:`MainWindow.show_stats` ``(field=None, *, scope=None)`` -- the node menu, the table header
and the schema view call it), Takroriy yozuvlar… (:meth:`MainWindow.show_dupes`: its export opens
the export dialog's record-set scope, its removal the bulk dialog's dedupe preview through
``BulkDialog.show_for(spec=, preview=True)``), JSON Schema ▸ Schema yaratish… / Schema bo'yicha
tekshirish…, JSON'ni ta'mirlash… (always; the current tab's file as the source), Faylni bo'lish…
(a document that finished indexing), Fayllarni birlashtirish… (always). Every tool window is
non-modal, opened through its module's ``show_*`` function (one per document / file /
application: an open one comes to the front) -- never the window-modal ``_show_dialog``; the file
askers go through :meth:`_ask_open_path` / :meth:`_ask_save_path` / :meth:`_ask_folder` /
:meth:`_ask_open_paths` (tests replace them). The tabs' "Ta'mirlash…" bars open the repair window
with the tab's format / record path (:meth:`MainWindow.show_repair`), whose "Joyini ko'rsatish"
shows the place in the tab's raw view (:meth:`MainWindow.show_repair_location`: by byte offset, or
by line for a prepared copy -- gzip, UTF-16); the compare windows reveal nodes with
:meth:`MainWindow.show_node`. A split dialog closes with its document's tab.

Long jobs (:meth:`MainWindow.running_jobs`, :data:`JOB_KINDS`): exports, splits / merges,
repairs, validations, schema generations, comparisons and search exports. Closing the window
while any of them runs asks first (closing stops them). Closing a tab lets the jobs reading its
document go on (they keep it open; the status bar says so) -- except a validation, whose window
closes with the document: the tab asks first then (headless: no question).
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import sys
import weakref
from typing import Any, Callable

from PySide6.QtCore import QByteArray, QObject, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QFont,
    QFontDatabase,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QStackedWidget,
    QStyle,
    QTabWidget,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from jsonreshape.errors import JsonReshapeError, UserInputError
from jsonreshape.gui import theme
from jsonreshape.gui.actions import (
    EDIT_COMMANDS,
    INSERT_KEYS,
    TYPE_KINDS,
    change_type_refusal,
    command_keys,
    edit_command,
    edit_command_state,
    edit_reason_text,
    run_edit_command,
    type_kinds,
    with_keys,
)
from jsonreshape.gui.doctab import VIEW_KINDS, DocumentTab, accepts_keyword, default_view_registry, view_label
from jsonreshape.gui.guistate import norm_path
from jsonreshape.gui.statusbar import StatusBar
from jsonreshape.gui.taskspanel import TasksPanel
from jsonreshape.gui.widgets import keep_alive, mono_font, open_log_folder, set_clipboard_text
from jsonreshape.i18n import t

log = logging.getLogger(__name__)

__all__ = ["DOCK_PANELS", "JOB_KINDS", "MainWindow", "PANEL_MODULES", "default_panel_factories", "factory_available",
           "parse_offset", "save_as_formats", "target_format"]

PANEL_MODULES: dict[str, tuple[str, str]] = {
    "search": ("jsonreshape.gui.searchpanel", "SearchPanel"),
    "filter": ("jsonreshape.gui.filterpanel", "FilterPanel"),
    "stats": ("jsonreshape.gui.statspanel", "StatsPanel"),
    "sort": ("jsonreshape.gui.sortdialog", "SortDialog"),
    "export": ("jsonreshape.gui.exportdialog", "ExportDialog"),
    "settings": ("jsonreshape.gui.settingsdialog", "SettingsDialog"),
    "bulk": ("jsonreshape.gui.bulkdialog", "BulkDialog"),
}
DOCK_PANELS = ("search", "filter", "stats")
#: Task kinds of the long background jobs the user started (stage 5 added file tools, repair, validation,
#: schema generation and comparison to the exports): closing the window would stop them, so it asks first.
JOB_KINDS = frozenset({"export", "split", "merge", "repair", "validate", "schema", "compare"})
OPEN_PATTERNS = "*.json *.jsonl *.ndjson *.json.gz *.geojson"
#: GUI state keys of this module (besides ``geometry`` / ``window_state`` / ``last_dir``).
STATE_SPLIT_VERTICAL = "split_vertical"
STATE_SECOND_PANE = "second_pane"
STATE_PATCH_DIR = "last_patch_dir"
#: Window-level editing shortcuts (§12.9; line edits keep their own Ctrl+Z / Ctrl+Y).
SAVE_KEYS = "Ctrl+S"
SAVE_AS_KEYS = "Ctrl+Shift+S"
UNDO_KEYS = ("Ctrl+Z",)
REDO_KEYS = ("Ctrl+Y", "Ctrl+Shift+Z")
BULK_KEYS = "Ctrl+H"
BOOKMARK_KEYS = "Ctrl+B"


def save_as_formats(kind: str | None) -> tuple[str, ...]:
    """The export formats (``writer.format_for_path`` names) whose file names keep a
    document of ``kind`` in its own format: JSON Lines -> ``jsonl``; concatenated values ->
    ``json`` / ``jsonl``; every other kind -> ``json``."""
    if kind == "jsonl":
        return ("jsonl",)
    if kind == "stream":
        return ("json", "jsonl")
    return ("json",)


def target_format(path: str | os.PathLike[str]) -> str | None:
    """The format a file name implies (a ``.gz`` suffix looked through); ``None`` when the
    extension says nothing (``.geojson``, ``.txt``, none)."""
    from jsonreshape import writer

    name = os.fspath(path)
    if name.lower().endswith(".gz"):
        name = name[:-3]
    return writer.format_for_path(name)


def _alive(obj: Any) -> bool:
    """``obj`` is a Qt object whose C++ side still exists."""
    if obj is None:
        return False
    try:
        import shiboken6

        return bool(shiboken6.isValid(obj))
    except Exception:  # pragma: no cover - shiboken always available with PySide6
        return True


def _view_record_selection(session: Any, view: Any) -> Any:
    """The records selected in ``view`` as a bulk scope (``None``: none): the view's own
    ``bulk_selection()`` when it has one; a table's selected rows (positions of the view);
    the records holding the tree's selected nodes. Positions of the state shown now."""
    if view is None:
        return None
    fn = getattr(view, "bulk_selection", None)
    if callable(fn):
        return fn()
    doc = getattr(session, "doc", None)
    if doc is None or getattr(doc, "kind", None) == "single":
        return None
    kind = getattr(view, "view_id", None)
    try:
        from jsonreshape.gui.bulkdialog import SelectionScope
    except ImportError:  # pragma: no cover - part of the package
        return None
    if kind == "table":
        grid = getattr(view, "grid", None)
        ranges = grid.selected_ranges() if grid is not None else []
        return SelectionScope.for_view_rows(session, ranges) if ranges else None
    if kind == "tree":
        keypaths = getattr(view, "selected_keypaths", None)
        kps = keypaths() if callable(keypaths) else None
        records = sorted({kp[0] for kp in kps or () if kp and isinstance(kp[0], int) and not isinstance(kp[0], bool)})
        if not records:
            return None
        edit = getattr(session, "edit", None)
        gen = getattr(edit, "applied_gen", None) if edit is not None else None
        if gen is None:
            gen = getattr(session, "data_gen", None)
        return SelectionScope(positions=records, gen=gen)
    return None


def _open_inline_editors(root: Any) -> list[Any]:
    """The inline editors open under ``root`` (:func:`jsonreshape.gui.editing.open_inline_editors`; review M3)."""
    from jsonreshape.gui.editing import open_inline_editors

    return open_inline_editors(root)


def _needs_close_answer(tab: Any) -> bool:
    """``tab`` must be asked before it closes / reopens: unsaved changes, or its controller runs a long
    operation whose result closing would drop (``EditController.needs_close_answer``; review CL-4)."""
    if tab is None or tab.closed:
        return False
    if tab.dirty:
        return True
    edit = tab.edit
    return edit is not None and bool(getattr(edit, "needs_close_answer", False))


def _is_job(task: Any) -> bool:
    """``task`` is a long background job the user started and closing the window would stop (:data:`JOB_KINDS`,
    visible, running) -- not a task a document recomputes (owned by its session: filter, sort, the schema scan,
    statistics) nor one the tab's close question covers (owned by its edit controller: saves, patch exports;
    review CL-4)."""
    if getattr(task, "kind", None) not in JOB_KINDS or getattr(task, "silent", False):
        return False
    if getattr(task, "state", "running") != "running":
        return False
    owner = getattr(task, "owner", None)
    if owner is None:
        return True
    from jsonreshape.gui.editing import EditController
    from jsonreshape.gui.session import DocSession

    return not isinstance(owner, (DocSession, EditController))


def export_icon(size_list: tuple[int, ...] = (16, 20, 24, 32, 48)) -> QIcon:
    """The toolbar icon of Export (review L9): a page with an arrow leaving it, in the theme's text and accent
    colors -- distinct from Save's disk."""
    fg = QColor(theme.color("fg"))
    accent = QColor(theme.color("accent"))
    icon = QIcon()
    for size in size_list:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            s = size / 16.0
            pen = QPen(fg, max(1.0, 1.1 * s))
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            page = QPainterPath()
            page.moveTo(10.5 * s, 6.0 * s)            # the right edge is open where the arrow leaves
            page.lineTo(10.5 * s, 4.0 * s)
            page.lineTo(8.0 * s, 1.5 * s)
            page.lineTo(2.5 * s, 1.5 * s)
            page.lineTo(2.5 * s, 14.5 * s)
            page.lineTo(10.5 * s, 14.5 * s)
            page.lineTo(10.5 * s, 11.0 * s)
            page.moveTo(8.0 * s, 1.5 * s)             # the folded corner
            page.lineTo(8.0 * s, 4.0 * s)
            page.lineTo(10.5 * s, 4.0 * s)
            painter.drawPath(page)
            arrow_pen = QPen(accent, max(1.2, 1.6 * s))
            arrow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            arrow_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(arrow_pen)
            arrow = QPainterPath()
            arrow.moveTo(5.5 * s, 8.5 * s)
            arrow.lineTo(14.5 * s, 8.5 * s)
            arrow.moveTo(11.5 * s, 5.5 * s)
            arrow.lineTo(14.5 * s, 8.5 * s)
            arrow.lineTo(11.5 * s, 11.5 * s)
            painter.drawPath(arrow)
        finally:
            painter.end()
        icon.addPixmap(pixmap)
    return icon


def _patterns(kind: str | None, compressed: bool) -> str:
    exts = (".jsonl", ".ndjson") if kind == "jsonl" else (".json",) if kind != "stream" else (".json", ".jsonl")
    gz = ".gz" if compressed else ""
    return " ".join(f"*{ext}{gz}" for ext in exts)


def _lazy_factory(module_name: str, class_name: str) -> Callable[..., Any]:
    def make(*args: Any, **kwargs: Any) -> Any:
        module = importlib.import_module(module_name)
        return getattr(module, class_name)(*args, **kwargs)

    make.__name__ = f"make_{class_name}"
    make.module_name = module_name  # type: ignore[attr-defined]
    return make


def factory_available(factory: Callable[..., Any] | None) -> bool:
    """False when ``factory`` is missing or is a lazy default whose module is not
    installed (checked without importing it)."""
    if factory is None:
        return False
    module_name = getattr(factory, "module_name", None)
    if module_name is None:
        return True
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


def default_panel_factories() -> dict[str, Callable[..., Any]]:
    """Factories importing the real panel / dialog modules on first use."""
    return {key: _lazy_factory(module, cls) for key, (module, cls) in PANEL_MODULES.items()}


def parse_offset(text: str) -> int:
    """A byte offset typed by the user: decimal (spaces, ``_`` and ``,`` ignored) or hex
    with ``0x``. ``UserInputError("gui.main.offset.invalid")`` otherwise."""
    raw = (text or "").strip().replace(" ", "").replace("_", "").replace(",", "").replace("\N{NO-BREAK SPACE}", "")
    try:
        value = int(raw, 16) if raw.lower().startswith("0x") else int(raw, 10)
    except ValueError:
        raise UserInputError("gui.main.offset.invalid", text=text) from None
    if value < 0:
        raise UserInputError("gui.main.offset.invalid", text=text)
    return value


def _key_sequences(item: Any) -> list[QKeySequence]:
    """The key sequences of a shortcut spec (text, QKeySequence or a standard key)."""
    if isinstance(item, QKeySequence.StandardKey):
        return list(QKeySequence.keyBindings(item))
    return [item if isinstance(item, QKeySequence) else QKeySequence(item)]


def _menu_text(text: str) -> str:
    return text.replace("&", "&&")


class MainWindow(QMainWindow):
    """See the module docstring."""

    tab_opened = Signal(object)            # DocumentTab
    tab_closed = Signal(object)            # DocumentTab (already shut down)
    current_tab_changed = Signal(object)   # DocumentTab | None
    closed = Signal()                      # the window closed (tabs closed, state saved)

    def __init__(self, app_ctx: Any, *, view_registry: dict[str, type] | None = None,
                 panel_factories: dict[str, Callable[..., Any]] | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app_ctx
        self.state = app_ctx.state
        self._registry = dict(view_registry) if view_registry is not None else default_view_registry()
        factories = default_panel_factories()
        factories.update(panel_factories or {})
        self._factories = factories
        self.acts: dict[str, QAction] = {}
        self.menus: dict[str, QMenu] = {}
        self.panels: dict[str, QWidget] = {}
        self.docks: dict[str, QDockWidget] = {}
        self.dialogs: dict[str, Any] = {}          # the last dialog of each kind (tests, reuse)
        self._available = {key: factory_available(f) for key, f in factories.items()}
        self._closing = False
        self._recent_origin: set[int] = set()      # ids of tabs opened from the recent files
        self._panel_sessions: dict[str, Any] = {}  # dock panel -> the session given to set_session
        # closing with unsaved changes (§12.9)
        self._asking: set[int] = set()              # ids of tabs whose close question is open
        self._close_confirmed = False               # the dirty tabs were answered: close for real
        self._close_chain = False                   # the window close asks the dirty tabs now
        self._quiet_close = False                   # close without questions (event loop ended)
        self._bulk: dict[int, Any] = {}             # id(session) -> its bulk operations dialog
        self.setObjectName("main_window")
        self.setAcceptDrops(True)

        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("document_tabs")
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideMiddle)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._on_current_changed)
        self.welcome = QLabel(t("gui.main.welcome"), self)
        self.welcome.setObjectName("welcome")
        self.welcome.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.welcome.setWordWrap(True)
        self.welcome.setEnabled(False)
        self.central = QStackedWidget(self)
        self.central.setObjectName("central")
        self.central.addWidget(self.welcome)
        self.central.addWidget(self.tabs)
        self.setCentralWidget(self.central)

        self.status = StatusBar(app_ctx, self)
        self.setStatusBar(self.status)
        self.status.tasks_clicked.connect(self.show_tasks)

        self._create_docks()
        self._create_actions()
        self._create_menus()
        self._create_toolbar()

        app_ctx.open_requested.connect(self._on_open_requested)
        app_ctx.settings_changed.connect(self._on_settings_changed)
        theme.notifier().theme_changed.connect(self._on_theme_changed)
        self.resize(1280, 820)
        self.restore_window_state()
        self._update_title()
        self._update_actions()

    # ================================================================== tabs
    def current_tab(self) -> DocumentTab | None:
        widget = self.tabs.currentWidget()
        return widget if isinstance(widget, DocumentTab) else None

    def tab_widgets(self) -> list[DocumentTab]:
        return [w for w in (self.tabs.widget(i) for i in range(self.tabs.count())) if isinstance(w, DocumentTab)]

    def current_session(self) -> Any:
        tab = self.current_tab()
        return tab.session if tab is not None else None

    def active_view(self) -> Any:
        tab = self.current_tab()
        return tab.active_view() if tab is not None else None

    def open_path(self, path: str | os.PathLike[str], options: dict[str, Any] | None = None, *,
                  focus_existing: bool = False, from_recent: bool = False) -> DocumentTab:
        """Open ``path`` in a new tab (``options``: ``record_path``, ``format``, ``ref``).
        With ``focus_existing`` and no options, a tab already showing the file is focused
        instead."""
        from jsonreshape.gui.session import DocSession

        path = os.path.abspath(os.fspath(path))
        options = dict(options or {})
        if focus_existing and not options:
            key = norm_path(path)
            for tab in self.tab_widgets():
                if norm_path(tab.session.path) == key:
                    self.tabs.setCurrentWidget(tab)
                    return tab
        session = DocSession(self.app, path, record_path=options.get("record_path"), format=options.get("format"))
        tab = DocumentTab(session, view_registry=self._registry, parent=self.tabs)
        tab.pending_ref = options.get("ref")              # revealed after the first open
        if bool(self.state.get(STATE_SPLIT_VERTICAL, False)):
            tab.set_orientation(Qt.Orientation.Vertical)
        if not bool(self.state.get(STATE_SECOND_PANE, True)):
            tab.set_second_pane_visible(False)
        # bound methods only: a connection must not keep a closed tab alive
        tab.title_changed.connect(self._on_tab_title_changed)
        tab.active_view_changed.connect(self._update_actions)
        tab.edit_state_changed.connect(self._on_tab_edit_state)
        tab.view_state_changed.connect(self._on_tab_edit_state)
        tab.filter_field_requested.connect(self.add_filter_condition)
        # stage 5: the bars' "Ta'mirlash…", the schema view's "Statistika" / "Takroriy yozuvlar…"
        tab.repair_requested.connect(self._on_tab_repair)
        tab.stats_requested.connect(self._on_tab_stats)
        tab.dupes_requested.connect(self._on_tab_dupes)
        session.state_changed.connect(self._update_actions)
        session.history_changed.connect(self._update_actions)
        session.view_changed.connect(self._update_actions)
        session.opened.connect(self._on_session_opened)
        session.open_failed.connect(self._on_session_open_failed)
        if from_recent:
            self._recent_origin.add(id(tab))
        index = self.tabs.addTab(tab, tab.title().replace("&", "&&"))
        self.tabs.setTabToolTip(index, tab.tooltip())
        self.tabs.setCurrentIndex(index)
        self.tab_opened.emit(tab)
        session.start()
        return tab

    def _tab_of(self, session: Any) -> DocumentTab | None:
        for tab in self.tab_widgets():
            if tab.session is session:
                return tab
        return None

    def _on_session_opened(self) -> None:
        tab = self._tab_of(self.sender())
        if tab is None:
            return
        ref, tab.pending_ref = tab.pending_ref, None
        self._tab_opened(tab, ref)

    def _on_session_open_failed(self, exc: BaseException) -> None:
        tab = self._tab_of(self.sender())
        if tab is not None:
            self._tab_open_failed(tab, exc)

    def _on_tab_title_changed(self) -> None:
        tab = self.sender()
        if isinstance(tab, DocumentTab):
            self._tab_title_changed(tab)

    def _on_tab_edit_state(self) -> None:
        """A tab's edit controller changed its state: the edit commands of the current tab."""
        if self.sender() is self.current_tab():
            self._update_actions()

    def _save_state(self) -> None:
        """Save the GUI state without blocking (a silent task when the context has one)."""
        saver = getattr(self.app, "save_state", None)
        if callable(saver):
            saver()
        else:  # pragma: no cover - a bare context
            self.state.save()

    def _tab_opened(self, tab: DocumentTab, ref: Any) -> None:
        if tab.closed:
            return
        self._recent_origin.discard(id(tab))
        try:
            self.state.add_recent(tab.session.path)
            self._save_state()
        except Exception:
            log.debug("could not update the recent files", exc_info=True)
        if ref is not None:
            tab.session.select(ref, reveal=True)
        self._update_actions()

    def _tab_open_failed(self, tab: DocumentTab, exc: BaseException) -> None:
        if tab.closed:
            return
        if id(tab) in self._recent_origin and isinstance(exc, JsonReshapeError) \
                and exc.key == "error.file_not_found":
            self._recent_origin.discard(id(tab))
            self.state.remove_recent(tab.session.path)
            self._save_state()
            self.app.show_status(t("gui.main.recent.missing", path=os.path.normpath(tab.session.path)), 8000)
        self._update_actions()

    def _tab_title_changed(self, tab: DocumentTab) -> None:
        index = self.tabs.indexOf(tab)
        if index < 0:
            return
        self.tabs.setTabText(index, tab.title().replace("&", "&&"))
        self.tabs.setTabToolTip(index, tab.tooltip())      # Save As moves the session to another path
        bar = self.tabs.tabBar()
        if tab.failed:
            bar.setTabTextColor(index, QColor(theme.color("danger")))
        else:
            bar.setTabTextColor(index, QColor())
        if tab is self.current_tab():
            self._update_title()
        self._update_actions()

    def close_tab(self, index: int | None = None) -> bool:
        """Close the tab at ``index`` (default: the current one): its views are shut down
        and its session closed. A tab with unsaved changes asks first (non-blocking: it is
        closed when the answer is Save -- after the save -- or Don't save). True when the
        tab was closed now."""
        if index is None:
            index = self.tabs.currentIndex()
        tab = self.tabs.widget(index) if index is not None and index >= 0 else None
        if not isinstance(tab, DocumentTab):
            return False
        if id(tab) in self._asking:
            return False                      # its question is open already
        if not self._confirm_stopping_jobs(tab):
            return False                      # a running validation of the document would be stopped: "no"
        closed: list[bool] = []

        def answered(ok: bool) -> None:
            self._asking.discard(id(tab))
            if ok and not tab.closed and self.tabs.indexOf(tab) >= 0:
                self._close_tab_now(tab)
                closed.append(True)

        self._asking.add(id(tab))
        # what is being typed first (M3), then the question (unsaved changes / a running operation: CL-4)
        if self._settle_edits(tab, lambda: self._confirm_tab(tab, "close", answered)) is False:
            self._asking.discard(id(tab))     # an editor refused (invalid value): the tab stays, nothing asked
            return False
        return bool(closed)

    def _confirm_tab(self, tab: DocumentTab, why: str, on_done: Callable[[bool], Any]) -> None:
        """Ask ``tab`` before it closes / reopens (``on_done(go_on)`` once). Its ``confirm_close`` asks only a
        document with unsaved changes; a clean document whose controller runs a long operation (Save As, patch
        import, ...) is asked by the controller directly ("wait / stop / cancel"; review CL-4)."""
        edit = tab.edit
        if edit is not None and not tab.dirty and bool(getattr(edit, "needs_close_answer", False)):
            ask = getattr(edit, "ask_close", None)
            if callable(ask):
                try:
                    ask(self, on_done, why=why)
                except Exception as exc:
                    log.exception("asking before closing %s failed", tab.session.name)
                    self.app.report_error(exc, self)
                    on_done(False)
                return
        tab.confirm_close(on_done, why=why, parent=self)

    def _settle_edits(self, tab: DocumentTab, then: Callable[[], Any]) -> Any:
        """Commit what the user is typing in ``tab`` before a save / close / reopen (review M3,
        :func:`jsonreshape.gui.editing.settle_edits`): ``then()`` runs once the editors' steps were applied (its
        result when at once, ``True`` when later); ``False``: refused (the status bar says why)."""
        from jsonreshape.gui.editing import settle_edits

        return settle_edits(tab, tab.edit, then, views=tab.views(), show_status=self.app.show_status)

    def _confirm_tabs(self, tabs: list[DocumentTab], why: str, on_done: Callable[[bool], Any]) -> None:
        """Ask the tabs with unsaved changes (or an open inline editor, or a running long
        operation) one after another (each is made current while it asks); ``on_done(True)``
        when all agreed, ``on_done(False)`` at the first Cancel (or an editor holding an
        invalid value). Answers may come at once (then ``on_done`` runs before this returns)."""
        pending = list(tabs)
        agreed: list[DocumentTab] = []             # answered yes (saved / discarded / kept) so far
        current: list[DocumentTab | None] = [None]

        def step(ok: bool = True) -> None:
            answered, current[0] = current[0], None
            if not ok:
                # a later Cancel: the tabs saved meanwhile stay open -> show their saved file
                self._reopen_saved(agreed)
                on_done(False)
                return
            if answered is not None:
                agreed.append(answered)
            while pending:
                tab = pending.pop(0)
                if tab.closed or not (_needs_close_answer(tab) or _open_inline_editors(tab)):
                    continue
                if self.tabs.indexOf(tab) >= 0:
                    self.tabs.setCurrentWidget(tab)
                current[0] = tab
                if self._settle_edits(tab, lambda t_=tab: self._confirm_tab(t_, why, step)) is False:
                    step(False)            # an editor refused (its value is invalid): like a Cancel
                return
            on_done(True)

        step(True)

    def _reopen_saved(self, tabs: list[DocumentTab]) -> None:
        """Tabs whose changes were saved by a close question (the edit session ended, the
        document not reopened yet) but that stay open because a later question was cancelled:
        open their saved file again (filter / sort kept)."""
        for tab in tabs:
            edit = tab.edit
            if tab.closed or edit is None or tab.dirty:
                continue
            if getattr(edit, "reason", "") == "closed" and not getattr(edit, "closed", False):
                tab.reopen(keep_view=True)

    def _close_tab_now(self, tab: DocumentTab) -> None:
        """Close ``tab`` without asking (its views shut down, its session closed)."""
        index = self.tabs.indexOf(tab)
        if index < 0:
            return
        self._recent_origin.discard(id(tab))
        self._asking.discard(id(tab))
        session = tab.session
        for key in DOCK_PANELS:
            if self._panel_sessions.get(key) is session:
                self._set_panel_session(key, None)
        # the jobs reading the document go on with it (a validation's window closes with the document)
        exports = len([job for job in self.running_jobs(session) if getattr(job, "kind", None) != "validate"])
        self._close_split_dialog(session)
        self.tabs.removeTab(index)
        tab.pending_ref = None
        tab.shutdown()              # closes the session (deleted later with its connections)
        tab.deleteLater()
        # forget the dialogs of the closed document (they would keep its session alive)
        for key, dialog in list(self.dialogs.items()):
            if getattr(dialog, "session", None) is session:
                del self.dialogs[key]
        bulk = self._bulk.pop(id(session), None)
        if bulk is not None and _alive(bulk):
            try:
                bulk.close()          # a bulk dialog of a closed document has nothing to act on
            except RuntimeError:  # pragma: no cover - deleted meanwhile
                pass
        if exports:
            # the jobs go on with the document they started with; their reports still appear
            self.app.show_status(t("gui.main.jobs_continue", count=exports, name=session.name), 8000)
        self.tab_closed.emit(tab)
        if self.tabs.count() == 0:
            self._on_current_changed(-1)

    def running_jobs(self, session: Any = None) -> list[Any]:
        """The long background jobs still running (:data:`JOB_KINDS`): exports, splits and merges, repairs,
        validations, schema generations, comparisons, search exports -- of every document, or (``session``)
        those reading ``session``'s document (``DocSession.document_users``; they keep it open, so all but a
        validation -- whose window closes with its document -- go on when its tab closes)."""
        if session is not None:
            users = getattr(session, "document_users", None)
            try:
                found = list(users()) if callable(users) else []
            except RuntimeError:  # pragma: no cover - the session is gone
                found = []
        else:
            tasks = getattr(self.app, "tasks", None)
            found = list(tasks.tasks()) if tasks is not None else []
        out: list[Any] = []
        for task in found:
            if _is_job(task) and not any(task is x for x in out):
                out.append(task)
        return out

    def _running_exports(self, session: Any = None) -> int:
        """The number of :meth:`running_jobs` (of ``session``'s document)."""
        return len(self.running_jobs(session))

    def _confirm_stopping_jobs(self, tab: DocumentTab) -> bool:
        """Closing ``tab`` would stop a validation of its document (the validation window closes with it): ask
        first ("no" keeps the tab). The other jobs go on with the document (status message after the close).
        Headless (tests, no answer possible): no question."""
        stopping = [job for job in self.running_jobs(tab.session) if getattr(job, "kind", None) == "validate"]
        if not stopping or getattr(self.app, "headless", False):
            return True
        return self._ask_yes_no(t("gui.main.jobs_stop.title"),
                                t("gui.main.jobs_stop", name=tab.session.name, count=len(stopping)))

    def _close_split_dialog(self, session: Any) -> None:
        """The split dialog of a closed document has nothing to plan any more (a running split goes on: its
        runner reports it)."""
        module = sys.modules.get("jsonreshape.gui.filetoolsdialog")
        dialog = module.SplitDialog.instance(session) if module is not None else None
        if dialog is not None and _alive(dialog):
            try:
                dialog.close()
            except RuntimeError:  # pragma: no cover - deleted meanwhile
                pass

    def close_all_tabs(self, on_done: Callable[[bool], Any] | None = None) -> bool:
        """Close every tab; tabs with unsaved changes ask first, one by one (a Cancel stops:
        the tabs not closed yet stay). True when all were closed now; ``on_done(all_closed)``
        once finished."""
        tabs = self.tab_widgets()
        result: list[bool] = []

        def finished(ok: bool) -> None:
            if ok:
                for tab in tabs:
                    if not tab.closed and self.tabs.indexOf(tab) >= 0:
                        self._close_tab_now(tab)
            result.append(bool(ok))
            if on_done is not None:
                on_done(bool(ok))

        self._confirm_tabs(tabs, "close", finished)
        return bool(result and result[0])

    def _on_current_changed(self, _index: int) -> None:
        if self._closing:
            return
        tab = self.current_tab()
        self.central.setCurrentWidget(self.tabs if self.tabs.count() else self.welcome)
        self.status.set_tab(tab)
        for key in DOCK_PANELS:
            self._set_panel_session(key, tab.session if tab is not None else None)
        self._update_title()
        self._update_actions()
        self.current_tab_changed.emit(tab)

    def _set_panel_session(self, key: str, session: Any) -> None:
        panel = self.panels.get(key)
        if panel is None:
            return
        if key in self._panel_sessions and self._panel_sessions[key] is session:
            return
        try:
            panel.set_session(session)
            self._panel_sessions[key] = session
        except Exception as exc:
            log.exception("%s panel set_session failed", key)
            self.app.report_error(exc, self)

    def _update_title(self) -> None:
        tab = self.current_tab()
        if tab is None:
            self.setWindowTitle(t("gui.main.app_name"))
        else:
            self.setWindowTitle(t("gui.main.window_title", name=tab.display_name()))

    def _on_open_requested(self, path: str, options: dict) -> None:
        self.open_path(path, options)

    # ================================================================== opening
    def open_dialog(self) -> list[DocumentTab]:
        """Ctrl+O: choose files (the last folder is remembered) and open them."""
        paths = self._ask_open_files()
        tabs = []
        for path in paths:
            tabs.append(self.open_path(path, focus_existing=True))
        if paths:
            try:
                self.state.set("last_dir", os.path.dirname(os.path.abspath(paths[0])))
            except Exception:
                log.debug("could not store last_dir", exc_info=True)
        return tabs

    def _ask_open_files(self) -> list[str]:
        """The open dialog (tests replace this method)."""
        filters = ";;".join([t("gui.main.filter.json", patterns=OPEN_PATTERNS), t("gui.main.filter.all")])
        paths, _selected = QFileDialog.getOpenFileNames(self, t("gui.main.open.title"),
                                                        str(self.state.get("last_dir") or ""), filters)
        return list(paths)

    def _ask_yes_no(self, title: str, text: str) -> bool:
        """A yes / no question (tests replace this method)."""
        box = QMessageBox(QMessageBox.Icon.Question, title, text,
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.button(QMessageBox.StandardButton.Yes).setText(t("gui.common.yes"))
        box.button(QMessageBox.StandardButton.No).setText(t("gui.common.no"))
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _ask_text(self, title: str, label: str, default: str = "") -> str | None:
        """A one-line input (tests replace this method); ``None`` when cancelled."""
        text, ok = QInputDialog.getText(self, title, label, QLineEdit.EchoMode.Normal, default)
        return text if ok else None

    def _ask_save_path(self, title: str, default: str, filters: str) -> str | None:
        """A save-file dialog (tests replace this method); ``None`` when cancelled."""
        path, _selected = QFileDialog.getSaveFileName(self, title, default, filters)
        return path or None

    def _ask_open_path(self, title: str, folder: str, filters: str) -> str | None:
        """An open-file dialog for one file (tests replace this method); ``None`` when
        cancelled."""
        path, _selected = QFileDialog.getOpenFileName(self, title, folder, filters)
        return path or None

    def _ask_open_paths(self, title: str, folder: str) -> list[str]:
        """An open-file dialog for several JSON files (the merge inputs; tests replace this method)."""
        filters = ";;".join([t("gui.main.filter.json", patterns=OPEN_PATTERNS), t("gui.main.filter.all")])
        paths, _selected = QFileDialog.getOpenFileNames(self, title, folder, filters)
        return list(paths)

    def _ask_folder(self, title: str, folder: str) -> str | None:
        """A folder dialog (the split output; tests replace this method); ``None`` when cancelled."""
        path = QFileDialog.getExistingDirectory(self, title, folder)
        return path or None

    def open_recent(self, path: str) -> DocumentTab:
        return self.open_path(path, focus_existing=True, from_recent=True)

    def _fill_recent_menu(self) -> None:
        menu = self.menus["recent"]
        menu.clear()
        files = self.state.recent_files()
        if not files:
            empty = menu.addAction(t("gui.main.recent.empty"))
            empty.setEnabled(False)
        for i, path in enumerate(files, start=1):
            name = os.path.basename(path) or path
            label = f"&{i} {_menu_text(name)}" if i < 10 else _menu_text(name)
            action = menu.addAction(label)
            action.setObjectName(f"recent_{i}")
            native = os.path.normpath(path)
            action.setToolTip(native)
            action.setStatusTip(native)
            action.setData(path)
            action.triggered.connect(lambda _checked=False, p=path: self.open_recent(p))
        menu.addSeparator()
        clear = menu.addAction(t("gui.main.recent.clear"))
        clear.setObjectName("recent_clear")
        clear.setEnabled(bool(files))
        clear.triggered.connect(self.clear_recent)

    def recent_actions(self) -> list[QAction]:
        """The file actions of the recent files menu (filled now)."""
        self._fill_recent_menu()
        return [a for a in self.menus["recent"].actions() if a.objectName().startswith("recent_")
                and a.objectName() != "recent_clear"]

    def clear_recent(self) -> None:
        self.state.clear_recent()
        self._save_state()

    def open_command_line(self, items: list[str]) -> list[DocumentTab]:
        """Open the files given on the command line: empty arguments are ignored, a file
        given twice is opened once (its tab focused), a folder is not opened (status
        message)."""
        tabs: list[DocumentTab] = []
        seen: set[str] = set()
        for item in items:
            text = os.fspath(item).strip() if item is not None else ""
            if not text:
                continue
            key = norm_path(text)
            if key in seen:
                continue
            seen.add(key)
            if os.path.isdir(text):
                self.app.show_status(t("gui.main.folder_ignored", path=os.path.normpath(os.path.abspath(text))), 8000)
                continue
            tabs.append(self.open_path(text, focus_existing=True))
        return tabs

    # -- drag & drop -----------------------------------------------------------------------------
    @staticmethod
    def dropped_paths(mime: Any) -> list[str]:
        if mime is None or not mime.hasUrls():
            return []
        return [url.toLocalFile() for url in mime.urls() if url.isLocalFile() and url.toLocalFile()]

    def dragEnterEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        if self.dropped_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        if self.dropped_paths(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        paths = self.dropped_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        folders = [p for p in paths if os.path.isdir(p)]
        for path in paths:
            if path not in folders:
                self.open_path(path, focus_existing=True)
        if folders:
            # a folder is not a JSON file: say so instead of opening a failed tab
            self.app.show_status(t("gui.main.folder_ignored", path=os.path.normpath(folders[0])), 8000)

    # ================================================================== docks
    def _create_docks(self) -> None:
        tasks_panel = TasksPanel(self.app, self)
        self.panels["tasks"] = tasks_panel
        self._add_dock("tasks", t("gui.main.dock.tasks"), tasks_panel, Qt.DockWidgetArea.BottomDockWidgetArea)
        areas = {"search": Qt.DockWidgetArea.BottomDockWidgetArea, "filter": Qt.DockWidgetArea.RightDockWidgetArea,
                 "stats": Qt.DockWidgetArea.RightDockWidgetArea}
        for key in DOCK_PANELS:
            factory = self._factories.get(key)
            if not factory_available(factory):
                log.warning("the %s panel is not available (its module is not installed)", key)
                continue
            try:
                panel = factory(self.app, self)
            except Exception:
                log.warning("the %s panel is not available", key, exc_info=True)
                continue
            self.panels[key] = panel
            self._add_dock(key, self._dock_title(key), panel, areas[key])
            if key == "stats" and hasattr(panel, "dupes_opener"):
                # "Takroriylar…" of the statistics opens the window's duplicates dialog (export / bulk wiring)
                panel.dupes_opener = self._open_dupes_for

    @staticmethod
    def _dock_title(key: str) -> str:
        return {"tasks": t("gui.main.dock.tasks"), "search": t("gui.main.dock.search"),
                "filter": t("gui.main.dock.filter"), "stats": t("gui.statspanel.dock.title")}.get(key, key)

    def _add_dock(self, key: str, title: str, widget: QWidget, area: Qt.DockWidgetArea) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(f"dock_{key}")
        dock.setWidget(widget)
        dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        self.addDockWidget(area, dock)
        dock.hide()
        self.docks[key] = dock
        return dock

    def _show_dock(self, key: str) -> Any:
        dock = self.docks.get(key)
        if dock is None:
            return None
        dock.show()
        dock.raise_()
        return self.panels.get(key)

    def show_tasks(self) -> Any:
        return self._show_dock("tasks")

    def selected_text(self) -> str | None:
        """The text selected in the document area: the active view's ``selected_text()``
        when it has one, else the selection of the focused text field inside a tab."""
        view = self.active_view()
        fn = getattr(view, "selected_text", None) if view is not None else None
        if callable(fn):
            try:
                text = fn()
            except Exception:
                log.debug("selected_text failed", exc_info=True)
                text = None
            if text:
                return str(text)
        focus = QApplication.focusWidget()
        if focus is None or not self.tabs.isAncestorOf(focus):
            return None
        if isinstance(focus, QLineEdit):
            return focus.selectedText() or None
        if isinstance(focus, (QPlainTextEdit, QTextEdit)):
            return focus.textCursor().selectedText().replace("\N{PARAGRAPH SEPARATOR}", "\n") or None
        return None

    def show_search(self, text: str | None = None) -> Any:
        """Ctrl+Shift+F: the search dock (shown first) with the focus in its text field,
        filled with the selected text of the document area when there is one."""
        if text is None:
            text = self.selected_text()
        tab = self.current_tab()
        if tab is not None:
            self._set_panel_session("search", tab.session)
        panel = self._show_dock("search")
        fn = getattr(panel, "focus_search", None)
        if callable(fn):
            fn(text)
        return panel

    def show_filter(self) -> Any:
        """Ctrl+Shift+L: the filter dock with the focus in the expression line."""
        tab = self.current_tab()
        if tab is not None:
            self._set_panel_session("filter", tab.session)
        panel = self._show_dock("filter")
        fn = getattr(panel, "focus_expression", None)
        if callable(fn):
            fn()
        return panel

    def add_filter_condition(self, path: str) -> Any:
        """SchemaView "filter: exists" → a condition in the filter panel."""
        panel = self.panels.get("filter")
        if panel is None:
            return None
        tab = self.current_tab()
        if tab is not None:
            self._set_panel_session("filter", tab.session)
        self._show_dock("filter")
        try:
            panel.add_condition(path, "exists", None)
        except Exception as exc:
            log.exception("add_condition failed")
            self.app.report_error(exc, self)
        return panel

    # ================================================================================== tools (stage 5)
    def _session_for(self, session: Any = None) -> Any:
        """``session`` (default: the current tab's) when it is open with a document, else ``None``."""
        if session is None:
            session = self.current_session()
        if session is None or getattr(session, "closed", False) or getattr(session, "doc", None) is None:
            return None
        return session

    def _sender_tab(self) -> DocumentTab | None:
        """The tab whose signal is being handled, made the current tab (``None``: closed / not a tab here)."""
        tab = self.sender()
        if not isinstance(tab, DocumentTab) or tab.closed or self.tabs.indexOf(tab) < 0:
            return None
        if tab is not self.current_tab():
            self.tabs.setCurrentWidget(tab)
        return tab

    def _path_askers(self) -> dict[str, Callable[..., Any]]:
        """The round-A dialogs' file askers ``fn(parent, title, start, filters)`` routed through this window's
        own (replaceable) :meth:`_ask_open_path` / :meth:`_ask_save_path`."""
        return {"ask_open_path": lambda _parent, title, start, filters: self._ask_open_path(title, start, filters),
                "ask_save_path": lambda _parent, title, start, filters: self._ask_save_path(title, start, filters)}

    def show_stats(self, field: Any = None, *, scope: str | None = None) -> Any:
        """Tools ▸ "Maydon statistikasi…" and every "Statistika" (node menu, table header, schema view): the
        statistics dock with the current document. A ``field`` (a text such as ``orders[*].sum`` or a node
        path: array indices become ``[*]``) is computed at once; without one the field box gets the focus.
        ``scope``: ``view`` | ``all`` (default: the panel's choice -- the current view). Returns the panel."""
        tab = self.current_tab()
        if tab is not None:
            self._set_panel_session("stats", tab.session)
        panel = self._show_dock("stats")
        if panel is None:
            return None
        try:
            if field is not None and not (isinstance(field, str) and not field.strip()):
                panel.show_field(field, scope=scope)
            else:
                setter = getattr(panel, "set_scope", None)
                if scope is not None and callable(setter):
                    setter(scope)
                combo = getattr(panel, "field_combo", None)
                if combo is not None:
                    combo.setFocus(Qt.FocusReason.OtherFocusReason)
        except Exception as exc:
            log.exception("showing the statistics failed")
            self.app.report_error(exc, self)
        return panel

    def _on_tab_stats(self, field: str) -> None:
        """The schema view's "Statistika" (double-click) in a tab: that tab's statistics of ``field``."""
        if self._sender_tab() is not None:
            self.show_stats(field)

    def _on_tab_dupes(self, field: str) -> None:
        """The schema view's "Takroriy yozuvlar…" in a tab: the duplicates dialog of that tab, key ``field``."""
        tab = self._sender_tab()
        if tab is not None:
            self.show_dupes(tab.session, key=field or None)

    def _open_dupes_for(self, session: Any, key: str) -> Any:
        """The statistics panel's "Takroriylar…" (``StatsPanel.dupes_opener``)."""
        return self.show_dupes(session, key=key)

    def show_dupes(self, session: Any = None, *, key: str | None = None, scope: str | None = None) -> Any:
        """Tools ▸ "Takroriy yozuvlar…" (also the schema view and the statistics panel): the duplicates dialog of
        ``session`` (default: the current document; one per document, non-modal; ``key`` / ``scope`` only
        prefill it -- it never starts a search by itself). Its "Eksport…" opens the export dialog with the
        duplicates as its record-set scope (the user's view is left alone), "Takroriylarni o'chirish…" the bulk
        dialog's dedupe preview (nothing changes until it is applied there)."""
        session = self._session_for(session)
        if session is None:
            return None
        from jsonreshape.gui.dupesdialog import show_dupes_dialog

        ref = weakref.ref(session)
        try:
            dialog = show_dupes_dialog(self, session, key=key, scope=scope,
                                       open_export=lambda records, label: self._dupes_export(ref, records, label),
                                       open_bulk=lambda spec, sc: self._dupes_bulk(ref, spec, sc))
        except Exception as exc:
            log.exception("opening the duplicates dialog failed")
            self.app.report_error(exc, self)
            return None
        self.dialogs["dupes"] = dialog
        return dialog

    def _dupes_export(self, ref: Any, records: Any, label: str) -> Any:
        session = ref()
        if session is None or getattr(session, "closed", False):
            records.close()            # the callee owns the set
            return None
        return self.show_export_records(session, records, label)

    def _dupes_bulk(self, ref: Any, spec: Any, scope: str) -> Any:
        session = ref()
        if session is None or getattr(session, "closed", False):
            return None
        return self.show_bulk(session, scope=scope, spec=spec, preview=True)

    def show_export_records(self, session: Any, records: Any, label: str) -> Any:
        """The export dialog of ``session`` with ``records`` (a RecordSet of positions of the state shown, e.g.
        the duplicates) as its scope: the dialog owns the set (closed with it); the user's view is not changed."""
        if self._session_for(session) is None:
            records.close()
            return None
        dialog = self._show_dialog("export", session, self, scope="records", records=records, records_label=label,
                                   selection=self._bulk_selection_source(session))
        if dialog is None:
            records.close()
        return dialog

    def show_generate_schema(self, session: Any = None) -> Any:
        """Tools ▸ JSON Schema ▸ "Schema yaratish…": the generate dialog of the document (one per document,
        non-modal; the file is asked through :meth:`_ask_save_path`)."""
        session = self._session_for(session)
        if session is None:
            return None
        from jsonreshape.gui.validatepanel import show_generate_dialog

        try:
            dialog = show_generate_dialog(session, self, ask_save_path=self._path_askers()["ask_save_path"])
        except Exception as exc:
            log.exception("opening the schema generation dialog failed")
            self.app.report_error(exc, self)
            return None
        self.dialogs["schema_generate"] = dialog
        return dialog

    def show_validate(self, session: Any = None, *, schema_path: str | None = None) -> Any:
        """Tools ▸ JSON Schema ▸ "Schema bo'yicha tekshirish…": the validation window of the document (one per
        document, non-modal; closed with its document)."""
        session = self._session_for(session)
        if session is None:
            return None
        from jsonreshape.gui.validatepanel import show_validate_dialog

        try:
            dialog = show_validate_dialog(session, self, schema_path=schema_path, **self._path_askers())
        except Exception as exc:
            log.exception("opening the validation window failed")
            self.app.report_error(exc, self)
            return None
        self.dialogs["schema_validate"] = dialog
        return dialog

    def show_repair(self, path: Any = None, hints: dict[str, Any] | None = None) -> Any:
        """Tools ▸ "JSON'ni ta'mirlash…" (the current tab's file, if any) and the bars' "Ta'mirlash…": the repair
        window of ``path`` (one per file, non-modal) preset with ``hints`` (the tab's format / record path).
        "Joyini ko'rsatish" -> :meth:`show_repair_location`; "open the repaired file" opens a new tab. The
        file is never repaired in place."""
        if path is None:
            tab = self.current_tab()
            if tab is not None:
                path = tab.session.path
                if hints is None:
                    hints = tab.repair_hints()
        from jsonreshape.gui.repairdialog import show_repair_dialog

        try:
            dialog = show_repair_dialog(self.app, os.fspath(path) if path else None, self, hints=dict(hints or {}),
                                        show_location=self.show_repair_location, **self._path_askers())
        except Exception as exc:
            log.exception("opening the repair window failed")
            self.app.report_error(exc, self)
            return None
        self.dialogs["repair"] = dialog
        return dialog

    def _on_tab_repair(self, path: str, hints: dict) -> None:
        """A bar's "Ta'mirlash…" / "Standart JSON'ga aylantirish…" in a tab."""
        if self._sender_tab() is not None:
            self.show_repair(path, hints)

    def show_repair_location(self, loc: Any) -> Any:
        """A place of a repair report (:class:`~jsonreshape.gui.repairdialog.RepairLocation` of the SOURCE file)
        in the raw view of the tab showing that file: by byte offset when the raw view shows the file itself,
        by line when it shows a prepared copy (gzip, UTF-16: other offsets); a status message when no tab shows
        the file. Returns the raw view (``None``)."""
        key = norm_path(loc.path)
        for tab in self.tab_widgets():
            if tab.closed or norm_path(tab.session.path) != key:
                continue
            doc = tab.session.doc
            if doc is None:
                continue
            self.tabs.setCurrentWidget(tab)
            self.raise_()
            self.activateWindow()
            try:
                same = norm_path(doc.data_path) == norm_path(doc.source_path)
            except Exception:  # pragma: no cover - a document without paths
                same = False
            if same:
                return tab.show_raw_at(int(loc.offset), max(1, int(loc.length)))
            view = tab.show_raw_at(0)
            goto_line = getattr(view, "goto_line", None)
            if callable(goto_line):
                try:
                    goto_line(max(1, int(loc.line)))
                except Exception as exc:
                    log.exception("goto_line failed")
                    self.app.report_error(exc, self)
            return view
        self.app.show_status(t("gui.repairdialog.show_location.none"), 6000)
        return None

    def show_split(self, source: Any = None) -> Any:
        """Tools ▸ "Faylni bo'lish…" (and the node menu's "Shu tugunni qismlarga bo'lish…" with a NodeRef
        ``source``): the split dialog of the current document (one per document, non-modal)."""
        session = self._session_for()
        if session is None:
            return None
        from jsonreshape.gui.filetoolsdialog import show_split_dialog

        try:
            dialog = show_split_dialog(self, session, source=source,
                                       ask_folder=lambda _parent, title, start: self._ask_folder(title, start))
        except Exception as exc:
            log.exception("opening the split dialog failed")
            self.app.report_error(exc, self)
            return None
        self.dialogs["split"] = dialog
        return dialog

    def _open_documents(self) -> list[Any]:
        """The documents of the open tabs (the merge dialog's "open tabs")."""
        return [tab.session for tab in self.tab_widgets() if not tab.closed]

    def show_merge(self) -> Any:
        """Tools ▸ "Fayllarni birlashtirish…": the merge dialog (one per application, non-modal); the current
        document is added as the first input."""
        session = self._session_for()
        from jsonreshape.gui.filetoolsdialog import show_merge_dialog

        try:
            dialog = show_merge_dialog(
                self, self.app, self._open_documents, inputs=[session] if session is not None else [],
                ask_files=lambda _parent, title, start: self._ask_open_paths(title, start),
                ask_output=lambda _parent, title, start: self._ask_save_path(title, start, ""))
        except Exception as exc:
            log.exception("opening the merge dialog failed")
            self.app.report_error(exc, self)
            return None
        self.dialogs["merge"] = dialog
        return dialog

    def show_compare_files(self) -> Any:
        """Tools ▸ "Ikki faylni taqqoslash…": choose two files / tabs (A = the current document); the compare
        window opens when accepted. Its "show in the tree" uses :meth:`show_node`."""
        from jsonreshape.gui import comparewindow

        try:
            dialog = comparewindow.show_compare_files(self, self.current_session(), reveal=self.show_node)
        except Exception as exc:
            log.exception("opening the compare dialog failed")
            self.app.report_error(exc, self)
            return None
        self.dialogs["compare"] = dialog
        return dialog

    def show_node(self, session: Any, ref: Any) -> bool:
        """Show ``ref`` of ``session`` in its tab (the compare windows' "show in the tree"): the tab becomes
        current, the window comes to the front, the node is selected and revealed. False when no tab of this
        window shows the session (closed)."""
        tab = self._tab_of(session)
        if tab is None or tab.closed or getattr(session, "closed", False) or ref is None:
            return False
        self.tabs.setCurrentWidget(tab)
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()
        session.select(ref, reveal=True)
        return True

    # ================================================================== actions
    def action(self, key: str) -> QAction:
        return self.acts[key]

    def _act(self, key: str, text: str, slot: Callable[..., Any] | None = None, *, shortcut: Any = None,
             checkable: bool = False, view_scoped: bool = False, tip: str | None = None,
             icon: QStyle.StandardPixmap | None = None) -> QAction:
        action = QAction(text, self)
        action.setObjectName(f"action_{key}")
        if shortcut is not None:
            # a key given twice to one action would be ambiguous (Qt then triggers nothing)
            unique: dict[str, QKeySequence] = {}
            for item in shortcut if isinstance(shortcut, (list, tuple)) else [shortcut]:
                for seq in _key_sequences(item):
                    if not seq.isEmpty():
                        unique.setdefault(seq.toString(), seq)
            action.setShortcuts(list(unique.values()))
        if checkable:
            action.setCheckable(True)
        action.setProperty("base_tip", tip or "")
        if tip:
            action.setToolTip(tip)
            action.setStatusTip(tip)
        if icon is not None:
            action.setIcon(self.style().standardIcon(icon))
        if slot is not None:
            if checkable:
                action.toggled.connect(slot)
            else:
                action.triggered.connect(lambda _checked=False, fn=slot: fn())
        if view_scoped:
            # active only while the focus is in the document area: the docks keep their keys
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.central.addAction(action)
        else:
            self.addAction(action)
        self.acts[key] = action
        return action

    def _display_act(self, key: str, text: str, slot: Callable[[], Any]) -> QAction:
        """An action that only lives in a menu: no shortcut is registered (a key in its
        text after a tab is shown for display; the views' grids handle that key)."""
        action = QAction(text, self)
        action.setObjectName(f"action_{key}")
        action.setProperty("base_tip", "")
        action.triggered.connect(lambda _checked=False, fn=slot: fn())
        self.acts[key] = action
        return action

    def _create_actions(self) -> None:
        sp = QStyle.StandardPixmap
        # -- file
        self._act("open", t("gui.main.act.open"), self.open_dialog, shortcut=QKeySequence.StandardKey.Open,
                  icon=sp.SP_DialogOpenButton, tip=t("gui.main.act.open.tip"))
        self._act("reopen", t("gui.main.act.reopen"), self.reopen_current, shortcut="F5", icon=sp.SP_BrowserReload,
                  tip=t("gui.main.act.reopen.tip"))
        self._act("record_path", t("gui.main.act.record_path"), self.choose_record_path)
        self._act("save", t("gui.main.act.save"), self.save_current, shortcut=SAVE_KEYS, icon=sp.SP_DriveFDIcon,
                  tip=t("gui.main.act.save.tip"))
        self._act("save_as", t("gui.main.act.save_as"), self.save_as_current, shortcut=SAVE_AS_KEYS,
                  tip=t("gui.main.act.save_as.tip"))
        # its own icon (a page with an arrow; the standard "save" pixmap looked like Save's disk: review L9)
        self._act("export", t("gui.main.act.export"), self.show_export, shortcut="Ctrl+E",
                  tip=t("gui.main.act.export.tip")).setIcon(export_icon())
        self._act("close_tab", t("gui.main.act.close_tab"), self.close_tab, shortcut=["Ctrl+W", "Ctrl+F4"])
        self._act("quit", t("gui.main.act.quit"), self.close, shortcut="Ctrl+Q")
        # -- edit: undo / redo (window level; a focused line edit keeps its own Ctrl+Z)
        # no standard pixmap fits (the arrows are Back / Forward): compact symbols in the toolbar
        self._act("undo", t("gui.main.act.undo"), self.undo, shortcut=list(UNDO_KEYS),
                  tip=t("gui.main.act.undo.tip")).setIconText("↶")
        self._act("redo", t("gui.main.act.redo"), self.redo, shortcut=list(REDO_KEYS),
                  tip=t("gui.main.act.redo.tip")).setIconText("↷")
        # the view edit commands: keys shown only (the grids handle them, §12.9)
        for cmd in EDIT_COMMANDS:
            self._display_act(f"edit_{cmd.name}", with_keys(t(cmd.text_key), cmd.keys),
                              lambda c=cmd: self._view_edit(c))
        for kind in TYPE_KINDS:
            self._display_act(f"edit_type_{kind}", t(f"gui.actions.edit.type.{kind}"),
                              lambda k=kind: self._view_edit("change_type", k))
        self._act("bulk", t("gui.main.act.bulk"), self.show_bulk, shortcut=BULK_KEYS, tip=t("gui.main.act.bulk.tip"))
        self._act("patch_import", t("gui.main.act.patch_import"), self.import_patch,
                  tip=t("gui.main.act.patch_import.tip"))
        self._act("patch_export", t("gui.main.act.patch_export"), self.export_patch,
                  tip=t("gui.main.act.patch_export.tip"))
        self._act("discard", t("gui.main.act.discard"), self.discard_changes, tip=t("gui.main.act.discard.tip"))
        self._act("copy_value", t("gui.main.act.copy_value"), lambda: self._view_call("copy_value", "json"),
                  shortcut=QKeySequence.StandardKey.Copy, view_scoped=True)
        self._act("copy_path", t("gui.main.act.copy_path"), lambda: self._view_call("copy_path", "dotted"),
                  shortcut="Ctrl+Shift+C", view_scoped=True)
        # -- view
        group = QActionGroup(self)
        group.setExclusive(True)
        for number, kind in enumerate(VIEW_KINDS, start=1):
            action = self._act(f"view_{kind}", view_label(kind), lambda k=kind: self.show_view_kind(k),
                               shortcut=f"Ctrl+{number}")
            action.setCheckable(True)
            group.addAction(action)
        self._view_group = group
        self._act("second_pane", t("gui.main.act.second_pane"), self.set_second_pane_visible, checkable=True)
        self._act("split_vertical", t("gui.main.act.split_vertical"), self.set_split_vertical, checkable=True)
        self._act("zoom_in", t("gui.main.act.zoom_in"), lambda: self._view_call("zoom", 1),
                  shortcut=[QKeySequence.StandardKey.ZoomIn, "Ctrl+="], view_scoped=True)
        self._act("zoom_out", t("gui.main.act.zoom_out"), lambda: self._view_call("zoom", -1),
                  shortcut=[QKeySequence.StandardKey.ZoomOut, "Ctrl+-"], view_scoped=True)
        self._act("zoom_reset", t("gui.main.act.zoom_reset"), lambda: self._view_call("zoom", 0),
                  shortcut="Ctrl+0", view_scoped=True)
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        current_theme = str(self.state.get("theme") or "system")
        for mode, label in (("system", t("gui.main.theme.system")), ("light", t("gui.main.theme.light")),
                            ("dark", t("gui.main.theme.dark"))):
            action = self._act(f"theme_{mode}", label, lambda m=mode: self.set_theme(m))
            action.setCheckable(True)
            action.setChecked(mode == current_theme)
            theme_group.addAction(action)
        self._theme_group = theme_group
        self._act("expand_all", t("gui.main.act.expand_all"), lambda: self._view_call("expand_all_current"),
                  shortcut=["*", QKeySequence(Qt.KeyboardModifier.KeypadModifier | Qt.Key.Key_Asterisk)],
                  view_scoped=True)
        self._act("collapse_all", t("gui.main.act.collapse_all"), lambda: self._view_call("collapse_all"))
        for level in range(1, 10):
            self._act(f"expand_level_{level}", t("gui.main.act.expand_level", level=level),
                      lambda n=level: self._view_call("expand_to_level", n))
        self._act("sort_keys", t("gui.main.act.sort_keys"), lambda on: self._view_call("toggle_sort_keys", bool(on)),
                  checkable=True, tip=t("gui.main.act.sort_keys.tip"))
        # -- navigate
        self._act("goto", t("gui.main.act.goto"), self.show_goto, shortcut="Ctrl+G", tip=t("gui.main.act.goto.tip"))
        self._act("back", t("gui.main.act.back"), self.go_back, shortcut="Alt+Left", icon=sp.SP_ArrowBack)
        self._act("forward", t("gui.main.act.forward"), self.go_forward, shortcut="Alt+Right",
                  icon=sp.SP_ArrowForward)
        self._act("bookmark_add", t("gui.main.act.bookmark_add"), self.add_bookmark, shortcut=BOOKMARK_KEYS)
        self._act("bookmarks", t("gui.main.act.bookmarks"), self.show_bookmarks, shortcut="Ctrl+Shift+D")
        self._act("goto_offset", t("gui.main.act.goto_offset"), self.goto_offset)
        # -- search
        self._act("quick_find", t("gui.main.act.quick_find"), lambda: self._view_call("quick_find"),
                  shortcut=QKeySequence.StandardKey.Find, view_scoped=True)
        self._act("find_next", t("gui.main.act.find_next"), lambda: self._view_call("find_next", False),
                  shortcut="F3", view_scoped=True)
        self._act("find_prev", t("gui.main.act.find_prev"), lambda: self._view_call("find_next", True),
                  shortcut="Shift+F3", view_scoped=True)
        self._act("search", t("gui.main.act.search"), lambda: self.show_search(), shortcut="Ctrl+Shift+F",
                  tip=t("gui.main.act.search.tip"))
        self._act("filter", t("gui.main.act.filter"), self.show_filter, shortcut="Ctrl+Shift+L",
                  tip=t("gui.main.act.filter.tip"))
        self._act("sort", t("gui.main.act.sort"), self.show_sort, tip=t("gui.main.act.sort.tip"))
        self._act("clear_view", t("gui.main.act.clear_view"), self.clear_view)
        # -- tools (stage 5, docs/ARCHITECTURE.md §14.9): compare, statistics, duplicates, JSON Schema,
        #    repair, split / merge; the windows they open are non-modal (one per document / file / app)
        self._act("compare_files", t("gui.compare.menu.files"), self.show_compare_files,
                  tip=t("gui.compare.menu.files.tip"))
        self._act("stats", t("gui.statspanel.action"), self.show_stats, tip=t("gui.statspanel.action.tip"))
        self._act("dupes", t("gui.dupesdialog.action"), self.show_dupes, tip=t("gui.dupesdialog.action.tip"))
        self._act("schema_generate", t("gui.validatepanel.menu.generate"), self.show_generate_schema,
                  tip=t("gui.validatepanel.menu.generate.tip"))
        self._act("schema_validate", t("gui.validatepanel.menu.validate"), self.show_validate,
                  tip=t("gui.validatepanel.menu.validate.tip"))
        self._act("repair", t("gui.repairdialog.menu.repair"), self.show_repair,
                  tip=t("gui.repairdialog.menu.repair.tip"))
        self._act("split_file", t("gui.filetools.action.split"), self.show_split,
                  tip=t("gui.filetools.action.split.tip"))
        self._act("merge_files", t("gui.filetools.action.merge"), self.show_merge,
                  tip=t("gui.filetools.action.merge.tip"))
        # -- tools / help
        self._act("settings", t("gui.main.act.settings"), self.show_settings)
        self._act("open_logs", t("gui.main.act.open_logs"), self.open_logs)
        self._act("environment", t("gui.main.act.environment"), self.show_environment)
        self._act("about", t("gui.main.act.about"), self.show_about)
        # dock toggles
        for key in ("tasks",) + DOCK_PANELS:
            dock = self.docks.get(key)
            if dock is not None:
                toggle = dock.toggleViewAction()
                toggle.setText(self._dock_title(key))
                toggle.setObjectName(f"action_dock_{key}")
                self.acts[f"dock_{key}"] = toggle

    def _create_menus(self) -> None:
        bar = self.menuBar()
        a = self.acts

        def menu(key: str, title: str, parent: Any = bar) -> QMenu:
            m = parent.addMenu(title)
            m.setObjectName(f"menu_{key}")
            m.setToolTipsVisible(True)
            self.menus[key] = m
            return m

        file_menu = menu("file", t("gui.main.menu.file"))
        file_menu.addAction(a["open"])
        recent = menu("recent", t("gui.main.menu.recent"), file_menu)
        recent.aboutToShow.connect(self._fill_recent_menu)
        file_menu.addAction(a["reopen"])
        file_menu.addAction(a["record_path"])
        file_menu.addSeparator()
        file_menu.addAction(a["save"])
        file_menu.addAction(a["save_as"])
        file_menu.addSeparator()
        file_menu.addAction(a["export"])
        file_menu.addSeparator()
        file_menu.addAction(a["close_tab"])
        file_menu.addAction(a["quit"])

        edit = menu("edit", t("gui.main.menu.edit"))
        edit.aboutToShow.connect(self._update_actions)
        edit.addAction(a["undo"])
        edit.addAction(a["redo"])
        edit.addSeparator()
        for name in ("edit_value", "edit_json", "rename"):
            edit.addAction(a[f"edit_{name}"])
        insert = menu("edit_insert", with_keys(t("gui.actions.edit.insert"), INSERT_KEYS), edit)
        for name in ("insert_child", "insert_before", "insert_after"):
            insert.addAction(a[f"edit_{name}"])
        for name in ("duplicate", "delete", "move_up", "move_down"):
            edit.addAction(a[f"edit_{name}"])
        types = menu("edit_type", t("gui.actions.edit.change_type"), edit)
        for kind in TYPE_KINDS:
            types.addAction(a[f"edit_type_{kind}"])
        edit.addAction(a["edit_paste"])
        edit.addSeparator()
        edit.addAction(a["copy_value"])
        edit.addAction(a["copy_path"])
        more = menu("copy_more", t("gui.main.menu.copy_more"), edit)
        from jsonreshape.gui.actions import add_copy_actions

        for key, action in add_copy_actions(more, self.active_view).items():
            self.acts[f"copy_more_{key}"] = action
        edit.addSeparator()
        for key in ("bulk", "patch_import", "patch_export"):
            edit.addAction(a[key])
        edit.addSeparator()
        edit.addAction(a["discard"])

        view = menu("view", t("gui.main.menu.view"))
        for kind in VIEW_KINDS:
            view.addAction(a[f"view_{kind}"])
        view.addSeparator()
        view.addAction(a["second_pane"])
        view.addAction(a["split_vertical"])
        view.addSeparator()
        view.addAction(a["expand_all"])
        view.addAction(a["collapse_all"])
        levels = menu("expand_level", t("gui.main.menu.expand_level"), view)
        for level in range(1, 10):
            levels.addAction(a[f"expand_level_{level}"])
        view.addAction(a["sort_keys"])
        view.addSeparator()
        view.addAction(a["zoom_in"])
        view.addAction(a["zoom_out"])
        view.addAction(a["zoom_reset"])
        themes = menu("theme", t("gui.main.menu.theme"), view)
        for mode in ("system", "light", "dark"):
            themes.addAction(a[f"theme_{mode}"])
        panels = menu("panels", t("gui.main.menu.panels"), view)
        for key in ("tasks",) + DOCK_PANELS:
            if f"dock_{key}" in a:
                panels.addAction(a[f"dock_{key}"])

        nav = menu("navigate", t("gui.main.menu.navigate"))
        for key in ("goto", "back", "forward"):
            nav.addAction(a[key])
        nav.addSeparator()
        nav.addAction(a["bookmark_add"])
        nav.addAction(a["bookmarks"])
        nav.addSeparator()
        nav.addAction(a["goto_offset"])

        search = menu("search", t("gui.main.menu.search"))
        for key in ("quick_find", "find_next", "find_prev"):
            search.addAction(a[key])
        search.addSeparator()
        for key in ("search", "filter", "sort", "clear_view"):
            search.addAction(a[key])

        tools = menu("tools", t("gui.main.menu.tools"))
        tools.aboutToShow.connect(self._update_actions)
        tools.addAction(a["compare_files"])
        tools.addSeparator()
        tools.addAction(a["stats"])
        tools.addAction(a["dupes"])
        schema = menu("json_schema", t("gui.validatepanel.menu.json_schema"), tools)
        schema.addAction(a["schema_generate"])
        schema.addAction(a["schema_validate"])
        tools.addAction(a["repair"])
        tools.addSeparator()
        tools.addAction(a["split_file"])
        tools.addAction(a["merge_files"])
        tools.addSeparator()
        for key in ("settings", "open_logs", "environment"):
            tools.addAction(a[key])

        help_menu = menu("help", t("gui.main.menu.help"))
        help_menu.addAction(a["about"])

    def _create_toolbar(self) -> None:
        bar = QToolBar(t("gui.main.toolbar"), self)
        bar.setObjectName("main_toolbar")
        bar.setMovable(True)
        a = self.acts
        for key in ("open", "save", "reopen", "export", None, "undo", "redo", None, "back", "forward", "goto", None,
                    "search", "filter", "sort", "clear_view", None, "second_pane"):
            if key is None:
                bar.addSeparator()
            else:
                bar.addAction(a[key])
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)
        self.toolbar = bar

    # -- enabling ---------------------------------------------------------------------------------
    def _update_actions(self, *_args: Any) -> None:
        if self._closing or not self.acts:
            return
        a = self.acts
        tab = self.current_tab()
        session = tab.session if tab is not None else None
        doc = session.doc if session is not None else None
        view = tab.active_view() if tab is not None else None
        has_tab = tab is not None
        has_doc = doc is not None
        records = has_doc and doc.kind != "single"

        def able(key: str, on: bool) -> None:
            action = a.get(key)
            if action is not None:
                action.setEnabled(bool(on))

        def view_has(method: str) -> bool:
            return view is not None and callable(getattr(view, method, None))

        for key in ("reopen", "close_tab", "record_path"):
            able(key, has_tab)
        able("export", has_doc and self._available.get("export", False))
        for key in ("copy_value", "copy_path", "zoom_in", "zoom_out", "zoom_reset", "quick_find", "find_next",
                    "find_prev"):
            able(key, view is not None)
        for key in list(a):
            if key.startswith("copy_more_"):
                able(key, view is not None)
        kinds = tab.available_kinds() if tab is not None else []
        active_kind = tab.active_kind() if tab is not None else None
        for kind in VIEW_KINDS:
            action = a[f"view_{kind}"]
            action.setEnabled(has_tab and kind in kinds)
            if kind == active_kind and not action.isChecked():
                action.setChecked(True)
        if active_kind is None:
            checked = self._view_group.checkedAction()
            if checked is not None:
                self._view_group.setExclusive(False)
                checked.setChecked(False)
                self._view_group.setExclusive(True)
        for key, on in (("second_pane", tab.second_pane_visible() if tab is not None else
                         bool(self.state.get(STATE_SECOND_PANE, True))),
                        ("split_vertical", tab.orientation() == Qt.Orientation.Vertical if tab is not None else
                         bool(self.state.get(STATE_SPLIT_VERTICAL, False)))):
            action = a[key]
            able(key, has_tab)
            if action.isChecked() != on:
                action.blockSignals(True)
                action.setChecked(on)
                action.blockSignals(False)
        able("expand_all", view_has("expand_all_current"))
        able("collapse_all", view_has("collapse_all"))
        for level in range(1, 10):
            able(f"expand_level_{level}", view_has("expand_to_level"))
        if "expand_level" in self.menus:
            self.menus["expand_level"].setEnabled(view_has("expand_to_level"))
        able("sort_keys", view_has("toggle_sort_keys"))
        for key in ("goto", "bookmark_add", "bookmarks"):
            able(key, has_doc)
        able("goto_offset", has_doc and "raw" in kinds)
        able("back", session is not None and session.can_back)
        able("forward", session is not None and session.can_forward)
        able("search", has_tab and "search" in self.panels)
        able("filter", records and "filter" in self.panels)
        able("sort", records and self._available.get("sort", False))
        able("clear_view", records and not session.view_is_base)
        able("settings", self._available.get("settings", False))
        # stage 5 tools
        able("compare_files", True)
        able("repair", True)
        able("merge_files", True)
        able("stats", has_doc and "stats" in self.panels)
        able("dupes", has_doc)
        able("schema_generate", has_doc)
        able("schema_validate", has_doc)
        able("split_file", has_doc and session.state == "ready")
        if "json_schema" in self.menus:
            self.menus["json_schema"].setEnabled(has_doc)
        self._update_edit_actions(tab, view)

    def _update_edit_actions(self, tab: DocumentTab | None, view: Any) -> None:
        """Save / undo / redo / the edit commands / bulk / patches / discard from the
        current tab's edit controller."""
        a = self.acts
        edit = tab.edit if tab is not None else None
        session = tab.session if tab is not None else None
        has = edit is not None
        dirty = has and bool(getattr(edit, "dirty", False))
        busy = has and bool(getattr(edit, "busy", False))
        available = has and bool(getattr(edit, "available", False))
        reason = edit_reason_text(edit) if has and not available else (t("gui.actions.edit.no_editing") if not has
                                                                         else "")

        def able(key: str, on: bool, why: str = "") -> None:
            action = a.get(key)
            if action is None:
                return
            action.setEnabled(bool(on))
            tip = why if (not on and why) else str(action.property("base_tip") or "")
            action.setToolTip(tip)        # "" = Qt's default (the text)
            action.setStatusTip(tip)

        busy_why = t("gui.actions.edit.busy")
        code = str(getattr(edit, "reason", "") or "") if has else ""
        # what the controller accepts (§12.9): a file that grew can be saved as another file
        # (Save offers it); the changes can be exported / discarded while the file changed
        savable = has and not busy and code in ("", "file_grown")
        keeps = has and not busy and code in ("", "file_changed", "file_grown")
        nothing = t("gui.main.save.nothing")
        able("save", savable and dirty, busy_why if busy else (reason if not savable else nothing))
        able("save_as", savable, busy_why if busy else reason)
        history = self._view_history(view)
        if history is not None:
            # the active view's own history (the structure builder, §13.5): the document's edit
            # undo is not reachable while that view is active
            can_undo = bool(getattr(history, "can_undo", False))
            can_redo = bool(getattr(history, "can_redo", False))
            able("undo", can_undo, t("gui.main.view_undo.none"))
            able("redo", can_redo, t("gui.main.view_redo.none"))
            undo_label = str(getattr(history, "undo_text", "") or "") if can_undo else ""
            redo_label = str(getattr(history, "redo_text", "") or "") if can_redo else ""
        else:
            can_undo = available and not busy and bool(getattr(edit, "can_undo", False))
            can_redo = available and not busy and bool(getattr(edit, "can_redo", False))
            able("undo", can_undo, busy_why if busy else reason)
            able("redo", can_redo, busy_why if busy else reason)
            undo_label = str(getattr(edit, "undo_text", "") or "") if can_undo else ""
            redo_label = str(getattr(edit, "redo_text", "") or "") if can_redo else ""
        a["undo"].setText(t("gui.main.act.undo_step", label=_menu_text(undo_label)) if undo_label
                          else t("gui.main.act.undo"))
        a["redo"].setText(t("gui.main.act.redo_step", label=_menu_text(redo_label)) if redo_label
                          else t("gui.main.act.redo"))
        editable = available and not busy
        able("bulk", editable and self._available.get("bulk", False), busy_why if busy else reason)
        able("patch_import", editable, busy_why if busy else reason)
        able("patch_export", keeps and dirty, busy_why if busy else (reason if not keeps else nothing))
        able("discard", keeps and dirty, busy_why if busy else (reason if not keeps else nothing))
        for cmd in EDIT_COMMANDS:
            ok, why = edit_command_state(session, view, cmd)
            action = a.get(f"edit_{cmd.name}")
            if action is not None:
                # the active view's own keys (the table moves no records, F2 renames there ...)
                text = with_keys(t(cmd.text_key), command_keys(view, cmd))
                if action.text() != text:
                    action.setText(text)
            able(f"edit_{cmd.name}", ok, why)
        ok, why = edit_command_state(session, view, "change_type")
        kinds = type_kinds(view) if ok else None
        for kind in TYPE_KINDS:
            # conversions §12.4 always refuses for the selected nodes are not offered (editors.type_refusal)
            refusal = change_type_refusal(kinds, kind) if ok else None
            able(f"edit_type_{kind}", ok and refusal is None, refusal or why)
        for key, names in (("edit_insert", ("insert_child", "insert_before", "insert_after")),
                           ("edit_type", tuple(f"type_{k}" for k in TYPE_KINDS))):
            menu = self.menus.get(key)
            if menu is not None:
                menu.setEnabled(any(a[f"edit_{n}"].isEnabled() for n in names))

    def _view_call(self, method: str, *args: Any) -> bool:
        """Call ``method(*args)`` on the active view when it has it."""
        view = self.active_view()
        fn = getattr(view, method, None) if view is not None else None
        if not callable(fn):
            return False
        try:
            fn(*args)
        except Exception as exc:
            log.exception("%s failed", method)
            self.app.report_error(exc, self)
        return True

    # ================================================================== commands
    def reopen_current(self) -> None:
        """F5: open the current file again (what is being typed is committed first, unsaved changes and a running
        long operation are asked about first)."""
        tab = self.current_tab()
        if tab is None:
            return

        def go() -> None:
            if tab.closed:
                return
            edit = tab.edit
            if edit is not None and not tab.dirty and bool(getattr(edit, "needs_close_answer", False)):
                # a clean document with a running Save As / patch import ...: the tab's reopen would not ask (CL-4)
                self._confirm_tab(tab, "reopen", lambda ok: tab.reopen() if ok and not tab.closed else None)
                return
            tab.reopen()

        self._settle_edits(tab, go)

    # -- editing (§12.9) -----------------------------------------------------------------
    def _current_edit(self) -> Any:
        tab = self.current_tab()
        edit = tab.edit if tab is not None else None
        if edit is None and tab is not None:
            self.app.show_status(t("gui.doctab.edit_unavailable"), 6000)
        return edit

    def _edit_call(self, method: str, *args: Any, **kwargs: Any) -> bool:
        """``edit.<method>(...)`` of the current tab's controller (errors reported)."""
        edit = self._current_edit()
        fn = getattr(edit, method, None) if edit is not None else None
        if not callable(fn):
            return False
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            log.exception("%s failed", method)
            self.app.report_error(exc, self)
            return False
        return True

    def save_current(self) -> bool:
        """Ctrl+S: save the current document in place (in its own format; the controller
        asks its questions first and reopens the document afterwards). An open inline editor
        is committed first (M3)."""
        tab = self.current_tab()
        if tab is None:
            return self._edit_call("save")
        return bool(self._settle_edits(tab, lambda: self._edit_call("save")))

    def _editors_ready(self, tab: DocumentTab) -> bool:
        """The open inline editors of ``tab`` can be committed (valid, no key check on its way, the controller not
        busy); else a status message says why (M3: checked before Save As asks for the file name)."""
        from jsonreshape.gui.editing import editors_ready

        return editors_ready(tab, tab.edit, show_status=self.app.show_status)

    def save_as_current(self, path: str | os.PathLike[str] | None = None) -> bool:
        """Ctrl+Shift+S: save the current document to another file in its own format and
        compression (``path`` asked when not given). A name of another format offers the
        export dialog instead. An open inline editor is committed first (M3)."""
        tab = self.current_tab()
        edit = tab.edit if tab is not None else None
        if tab is None or edit is None:
            if tab is not None:
                self.app.show_status(t("gui.doctab.edit_unavailable"), 6000)
            return False
        if not self._editors_ready(tab):
            return False
        doc = tab.session.doc
        kind = getattr(getattr(doc, "info", None), "kind", None) if doc is not None else None
        compression = getattr(getattr(doc, "info", None), "compression", None) if doc is not None else None
        if path is None:
            patterns = _patterns(kind, bool(compression))
            filters = ";;".join([t("gui.main.save_as.filter", patterns=patterns), t("gui.main.filter.all")])
            path = self._ask_save_path(t("gui.main.save_as.title", name=tab.session.name), tab.session.path,
                                       filters)
            if not path:
                return False
        path = os.path.abspath(os.fspath(path))
        fmt = target_format(path)
        if fmt is not None and fmt not in save_as_formats(kind):
            if self._ask_yes_no(t("gui.main.save_as.other_format.title"),
                                t("gui.main.save_as.other_format", ext=os.path.splitext(path)[1],
                                  fmt=t(f"gui.export.format.{fmt}"))):
                dialog = self.show_export(mapping=False)       # Save As: the document as it is
                setter = getattr(dialog, "set_output_path", None)
                if callable(setter):
                    setter(path)
            return False
        target = path
        return bool(self._settle_edits(tab, lambda: self._edit_call("save_as", target)))

    def _view_history(self, view: Any = None) -> Any:
        """The active view when it has an undo history of its own (the structure builder:
        ``undo()`` / ``redo()`` + ``can_undo`` / ``can_redo``), else ``None``."""
        if view is None:
            view = self.active_view()
        if view is not None and callable(getattr(view, "undo", None)) and callable(getattr(view, "redo", None)):
            return view
        return None

    def undo(self) -> bool:
        """Ctrl+Z: take the last step of the current document back -- or of the active view's
        own history (the structure builder) while that view is active (§13.5)."""
        history = self._view_history()
        if history is not None:
            return self._view_call("undo")
        return self._edit_call("undo")

    def redo(self) -> bool:
        """Ctrl+Y / Ctrl+Shift+Z: do the undone step again (the active view's own history first)."""
        history = self._view_history()
        if history is not None:
            return self._view_call("redo")
        return self._edit_call("redo")

    def _view_edit(self, command: Any, *args: Any) -> bool:
        """An edit command of the Edit menu on the active view."""
        view = self.active_view()
        if view is None:
            return False
        cmd = edit_command(command) if isinstance(command, str) and command != "change_type" else command
        return run_edit_command(view, cmd, *args)

    def show_bulk(self, session: Any = None, *, scope: str | None = None, spec: Any = None,
                  preview: bool = False) -> Any:
        """Ctrl+H: the bulk operations dialog of the current document -- or of ``session`` (the
        duplicates dialog's "remove the duplicates") -- one per document: an open one is brought to
        the front. Its scope starts at ``scope``, else the active view's selected records when more
        than one is selected, else the filtered view, else all records; "selected records" asks the
        active view at the time it is needed. ``spec`` (a ``BulkSpec``) fills the form and ``preview``
        starts its preview at once (never while a preview of the open dialog runs)."""
        if session is None:
            tab = self.current_tab()
            session = tab.session if tab is not None else None
        else:
            tab = self._tab_of(session)
        if session is None or getattr(session, "closed", False) or session.doc is None:
            return None
        factory = self._factories.get("bulk")
        if factory is None:
            return None
        selection = self._bulk_selection_source(session)
        if scope is None:
            scope = self._bulk_scope_default(tab) if tab is not None else "all"
        try:
            target = getattr(factory, "module_name", None)
            cls = getattr(importlib.import_module(target), PANEL_MODULES["bulk"][1]) if target is not None else None
            show_for = getattr(cls, "show_for", None) if cls is not None else None
            if callable(show_for):
                # the dialog keeps one per session itself (also when a view opened it)
                kwargs = {"parent": self, "scope_default": scope, "selection": selection}
                if (spec is not None or preview) and accepts_keyword(show_for, "spec"):
                    kwargs.update(spec=spec, preview=preview)
                    spec, preview = None, False          # applied by show_for
                dialog = show_for(session, **kwargs)
            else:
                dialog = self._bulk.get(id(session))
                if dialog is not None and _alive(dialog) and getattr(dialog, "session", session) is session:
                    for name, args in (("set_selection_source", (selection,)), ("refresh_scope", ())):
                        fn = getattr(dialog, name, None)
                        if callable(fn):
                            fn(*args)
                    dialog.show()
                    dialog.raise_()
                    dialog.activateWindow()
                    self._apply_bulk_spec(dialog, spec, scope, preview)
                    return dialog
                kwargs: dict[str, Any] = {"scope_default": scope}
                if accepts_keyword(factory, "selection"):
                    kwargs["selection"] = selection
                with_parent = accepts_keyword(factory, "parent")
                if with_parent:
                    kwargs["parent"] = self
                dialog = factory(session, **kwargs)
                if not with_parent and isinstance(dialog, QWidget) and dialog.parent() is None:
                    dialog.setParent(self, dialog.windowFlags() | Qt.WindowType.Window)
                if isinstance(dialog, QWidget):
                    dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
                    dialog.show()
            self._apply_bulk_spec(dialog, spec, scope, preview)
        except Exception as exc:
            log.exception("opening the bulk operations dialog failed")
            self.app.report_error(exc, self)
            return None
        if self._bulk.get(id(session)) is not dialog:
            self._bulk[id(session)] = dialog
            if isinstance(dialog, QObject):
                dialog.destroyed.connect(lambda _obj=None, k=id(session), d=dialog: self._forget_bulk(k, d))
        self.dialogs["bulk"] = dialog
        return dialog

    @staticmethod
    def _apply_bulk_spec(dialog: Any, spec: Any, scope: str | None, preview: bool) -> None:
        """Fill a bulk dialog without ``show_for(spec=, preview=)`` (an older / stub dialog): the operation,
        the scope and the preview -- not while its own preview runs."""
        if dialog is None or (spec is None and not preview) or getattr(dialog, "task", None) is not None:
            return
        if spec is not None and callable(getattr(dialog, "set_spec", None)):
            dialog.set_spec(spec)
        if scope and callable(getattr(dialog, "set_scope_choice", None)):
            dialog.set_scope_choice(scope)
        if preview and callable(getattr(dialog, "preview", None)):
            dialog.preview()

    def _bulk_scope_default(self, tab: DocumentTab) -> str:
        session = tab.session
        doc = session.doc
        if doc is None or doc.kind == "single":
            return "all"
        selected = _view_record_selection(session, tab.active_view())
        try:
            many = selected is not None and len(selected) > 1
        except TypeError:
            many = False
        if many:
            return "selected"
        return "all" if session.view_is_base else "view"

    def _bulk_selection_source(self, session: Any) -> Callable[[], Any]:
        """The bulk dialog's ``selection`` callback: the selected records of the session's
        tab's active view when it is asked (``None``: no selection)."""
        ref = weakref.ref(session)

        def selection() -> Any:
            s = ref()
            if s is None or getattr(s, "closed", False):
                return None
            tab = self._tab_of(s)
            return _view_record_selection(s, tab.active_view() if tab is not None else None)

        return selection

    def _forget_bulk(self, key: int, dialog: Any) -> None:
        if self._bulk.get(key) is dialog:
            del self._bulk[key]
        self._forget_dialog("bulk", dialog)

    def _patch_dir(self, session: Any) -> str:
        return str(self.state.get(STATE_PATCH_DIR) or os.path.dirname(session.path))

    def _remember_patch_dir(self, path: str) -> None:
        try:
            self.state.set(STATE_PATCH_DIR, os.path.dirname(os.path.abspath(path)))
        except Exception:
            log.debug("could not store the patch folder", exc_info=True)

    def import_patch(self, path: str | os.PathLike[str] | None = None) -> bool:
        """Edit ▸ Import JSON Patch…: apply an RFC 6902 patch file to the current document
        (one undo step)."""
        tab = self.current_tab()
        if tab is None or tab.edit is None:
            self._current_edit()
            return False
        if path is None:
            path = self._ask_open_path(t("gui.main.patch.import.title"), self._patch_dir(tab.session),
                                       t("gui.main.patch.filter"))
            if not path:
                return False
        path = os.path.abspath(os.fspath(path))
        self._remember_patch_dir(path)
        return self._edit_call("import_patch", path)

    def export_patch(self, path: str | os.PathLike[str] | None = None) -> bool:
        """Edit ▸ Export changes as JSON Patch…: the unsaved changes as an RFC 6902 patch."""
        tab = self.current_tab()
        if tab is None or tab.edit is None:
            self._current_edit()
            return False
        if path is None:
            stem = os.path.splitext(tab.session.name)[0] or "changes"
            if stem.lower().endswith(".json"):
                stem = stem[:-5]
            default = os.path.join(self._patch_dir(tab.session), f"{stem}.patch.json")
            path = self._ask_save_path(t("gui.main.patch.export.title"), default, t("gui.main.patch.filter"))
            if not path:
                return False
        path = os.path.abspath(os.fspath(path))
        self._remember_patch_dir(path)
        return self._edit_call("export_patch", path)

    def discard_changes(self) -> bool:
        """Edit ▸ Discard all changes: the controller asks, then deletes the edit layer."""
        return self._edit_call("discard", self)

    def choose_record_path(self) -> Any:
        tab = self.current_tab()
        if tab is None:
            return None
        dialog = tab.open_record_path_dialog()
        self.dialogs["record_path"] = dialog
        return dialog

    def show_view_kind(self, kind: str) -> Any:
        """Ctrl+1..6: show ``kind`` in the active pane."""
        tab = self.current_tab()
        if tab is None:
            return None
        view = tab.set_active_view_kind(kind)
        self._update_actions()
        return view

    def set_second_pane_visible(self, on: bool) -> None:
        on = bool(on)
        tab = self.current_tab()
        if tab is not None:
            tab.set_second_pane_visible(on)
        self.state.set(STATE_SECOND_PANE, on)
        self._update_actions()

    def set_split_vertical(self, on: bool) -> None:
        on = bool(on)
        tab = self.current_tab()
        if tab is not None:
            tab.set_orientation(Qt.Orientation.Vertical if on else Qt.Orientation.Horizontal)
        self.state.set(STATE_SPLIT_VERTICAL, on)
        self._update_actions()

    def set_theme(self, mode: str) -> None:
        """System / light / dark theme (remembered)."""
        app = QApplication.instance()
        if app is not None:
            theme.apply_theme(app, mode)
        try:
            self.state.set("theme", mode)
            self._save_state()
        except Exception:
            log.debug("could not store the theme", exc_info=True)
        action = self.acts.get(f"theme_{mode}")
        if action is not None and not action.isChecked():
            action.setChecked(True)

    def _on_theme_changed(self) -> None:
        """The drawn icons follow the theme's colors."""
        action = self.acts.get("export")
        if action is not None and not self._closing:
            action.setIcon(export_icon())

    def _on_settings_changed(self) -> None:
        mode = str(self.state.get("theme") or "system")
        action = self.acts.get(f"theme_{mode}")
        if action is not None and not action.isChecked():
            action.setChecked(True)
        self.apply_ui_font()
        self.status.update_metrics()

    def apply_ui_font(self) -> bool:
        """Use the ``ui_size`` GUI setting for the application font (0: the system UI
        font); True when the font changed."""
        app = QApplication.instance()
        if app is None:  # pragma: no cover
            return False
        try:
            size = float(self.state.get("ui_size") or 0)
        except (TypeError, ValueError):
            size = 0.0
        if size > 0:
            font = QFont(QApplication.font())
            font.setPointSizeF(min(size, 72.0))
        else:
            font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
        if QApplication.font() == font:
            return False
        QApplication.setFont(font)
        return True

    def show_export(self, *, mapping: bool = True) -> Any:
        """Ctrl+E: the export dialog; while the structure builder is the active view, its
        export with the mapping (§13.5: the dialog in mapping mode) -- not with ``mapping=False``
        (Save As in another format exports the document as it is)."""
        tab = self.current_tab()
        if tab is None or tab.session.doc is None:
            return None
        view = tab.active_view()
        mapped = getattr(view, "export_with_mapping", None) if view is not None and mapping else None
        if callable(mapped):
            try:
                return mapped()
            except Exception as exc:
                log.exception("export_with_mapping failed")
                self.app.report_error(exc, self)
                return None
        columns = None
        table = tab.table_view()
        fn = getattr(table, "visible_column_paths", None)
        if callable(fn):
            try:
                columns = list(fn()) or None
            except Exception:
                log.exception("visible_column_paths failed")
        kwargs: dict[str, Any] = {"columns": columns, "scope": "view"}
        if accepts_keyword(self._factories.get("export"), "selection"):
            # "selected records": the same selection source as the bulk dialog's (asked when needed)
            kwargs["selection"] = self._bulk_selection_source(tab.session)
        return self._show_dialog("export", tab.session, self, **kwargs)

    def show_sort(self) -> Any:
        session = self.current_session()
        if session is None or session.doc is None:
            return None
        return self._show_dialog("sort", session, self)

    def show_settings(self) -> Any:
        return self._show_dialog("settings", self.app, self)

    def _show_dialog(self, key: str, *args: Any, **kwargs: Any) -> Any:
        factory = self._factories.get(key)
        if factory is None:
            return None
        try:
            dialog = factory(*args, **kwargs)
        except Exception as exc:
            log.exception("opening the %s dialog failed", key)
            self.app.report_error(exc, self)
            return None
        self.dialogs[key] = dialog
        if isinstance(dialog, QObject):
            # a closed (deleted) dialog is forgotten: its Python object would keep what it
            # references alive (an export dialog: its finished job, task and outcome)
            dialog.destroyed.connect(lambda _obj=None, k=key, d=dialog: self._forget_dialog(k, d))
        if isinstance(dialog, QDialog):
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.open()
        elif isinstance(dialog, QWidget):
            dialog.show()
        return dialog

    def _forget_dialog(self, key: str, dialog: Any) -> None:
        if self.dialogs.get(key) is dialog:
            del self.dialogs[key]

    def clear_view(self) -> None:
        """Clear the filter, quick filters, sort and shown records."""
        session = self.current_session()
        if session is not None:
            session.reset_view()
        self._update_actions()

    # -- navigation ------------------------------------------------------------------------------
    def show_goto(self, text: str = "") -> Any:
        session = self.current_session()
        if session is None or session.doc is None:
            return None
        from jsonreshape.gui.navigation import GoToDialog

        dialog = GoToDialog(session, self, text=text)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.open()
        self.dialogs["goto"] = dialog
        return dialog

    def show_bookmarks(self) -> Any:
        session = self.current_session()
        if session is None or session.doc is None:
            return None
        from jsonreshape.gui.navigation import BookmarksDialog

        dialog = BookmarksDialog(session, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.open()
        self.dialogs["bookmarks"] = dialog
        return dialog

    def add_bookmark(self) -> Any:
        """Ctrl+B: bookmark the current node (name asked; default = its location)."""
        session = self.current_session()
        if session is None or session.doc is None:
            return None
        ref = session.current
        if ref is None:
            self.app.show_status(t("gui.main.bookmark.no_node"))
            return None
        default = session.location_text(ref)
        name = self._ask_text(t("gui.main.bookmark.title"), t("gui.main.bookmark.label"), default)
        if name is None:
            return None
        bookmark = session.add_bookmark(name, ref)
        self.app.show_status(t("gui.main.bookmark.added", name=bookmark.name))
        return bookmark

    def go_back(self) -> None:
        session = self.current_session()
        if session is not None:
            session.back()

    def go_forward(self) -> None:
        session = self.current_session()
        if session is not None:
            session.forward()

    def goto_offset(self, offset: int | None = None) -> Any:
        """Show the raw view of the active pane at a byte offset (asked when not given)."""
        tab = self.current_tab()
        if tab is None or tab.session.doc is None:
            return None
        if offset is None:
            text = self._ask_text(t("gui.main.offset.title"), t("gui.main.offset.label"), "")
            if text is None or not text.strip():
                return None
            try:
                offset = parse_offset(text)
            except UserInputError as exc:
                self.app.report_error(exc, self)
                return None
        return tab.show_raw_at(offset, 1, pane=tab.active_pane())

    # -- tools / help ----------------------------------------------------------------------------
    def open_logs(self) -> None:
        try:
            open_log_folder()
        except Exception as exc:
            log.exception("could not open the log folder")
            self.app.report_error(exc, self)

    def show_environment(self) -> Any:
        """The environment report (computed in a silent task)."""
        from jsonreshape.env import check_environment, format_environment

        def work(ctx: Any) -> str:
            return format_environment(check_environment(log_result=False))

        return self.app.tasks.run(work, title=t("gui.main.env.task"), kind="env", owner=self, silent=True,
                                  on_done=self._environment_ready,
                                  on_error=lambda exc: self.app.report_error(exc, self))

    def _environment_ready(self, text: str) -> QDialog:
        dialog = QDialog(self)
        dialog.setObjectName("environment_dialog")
        dialog.setWindowTitle(t("gui.main.env.title"))
        layout = QVBoxLayout(dialog)
        box = QPlainTextEdit(dialog)
        box.setReadOnly(True)
        box.setFont(mono_font())
        box.setPlainText(text)
        layout.addWidget(box)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(t("gui.common.close"))
        copy = buttons.addButton(t("gui.common.copy"), QDialogButtonBox.ButtonRole.ActionRole)
        copy.clicked.connect(lambda: set_clipboard_text(text))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(620, 420)
        keep_alive(dialog)
        dialog.open()
        self.dialogs["environment"] = dialog
        return dialog

    def about_text(self) -> str:
        from jsonreshape import __version__

        try:
            from jsonreshape import core

            backend = str(getattr(core, "BACKEND", "none"))
        except Exception:
            backend = "none"
        backend_text = {"rust": t("env.backend.rust"), "python": t("env.backend.python")}.get(
            backend, t("env.backend.none"))
        return t("gui.main.about.text", version=__version__, backend=backend_text)

    def show_about(self) -> QMessageBox:
        box = QMessageBox(QMessageBox.Icon.Information, t("gui.main.about.title"), self.about_text(),
                          QMessageBox.StandardButton.Ok, self)
        box.setObjectName("about_box")
        box.setTextFormat(Qt.TextFormat.PlainText)
        if not self.windowIcon().isNull():
            box.setIconPixmap(self.windowIcon().pixmap(64, 64))
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.open()
        self.dialogs["about"] = box
        return box

    # ================================================================== window state
    def restore_window_state(self) -> None:
        geometry = self.state.get("geometry")
        if isinstance(geometry, str) and geometry:
            try:
                self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
            except Exception:
                log.debug("restoring the geometry failed", exc_info=True)
        window_state = self.state.get("window_state")
        if isinstance(window_state, str) and window_state:
            try:
                self.restoreState(QByteArray.fromBase64(window_state.encode("ascii")))
            except Exception:
                log.debug("restoring the window state failed", exc_info=True)

    def save_window_state(self) -> None:
        try:
            self.state.set("geometry", bytes(self.saveGeometry().toBase64().data()).decode("ascii"))
            self.state.set("window_state", bytes(self.saveState().toBase64().data()).decode("ascii"))
        except Exception:
            log.exception("saving the window state failed")

    def _close_after_answers(self) -> None:
        if not self._closing:
            self.close()

    def close_without_questions(self) -> bool:
        """Close at once, without asking about unsaved changes or running exports (the edit
        layers stay for recovery): when nobody can answer any more (the event loop ended)."""
        self._quiet_close = True
        return self.close()

    def closeEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        """Ask about running exports and -- tab by tab -- about unsaved changes (the close
        is ignored meanwhile and done again once every tab answered), then save the window
        state, close the tabs (sessions), cancel the background tasks (waiting briefly) and
        save the GUI state."""
        if self._closing:
            event.accept()
            return
        if not self._close_confirmed and not self._quiet_close:
            if self._close_chain:          # the questions are open already
                event.ignore()
                return
            # exports, splits / merges, repairs, validations, generations, comparisons: closing stops them
            exports = self._running_exports()
            if exports and not getattr(self.app, "headless", False) \
                    and not self._ask_yes_no(t("gui.main.jobs_running.title"),
                                             t("gui.main.jobs_running", count=exports)):
                event.ignore()
                return
            # tabs with unsaved changes, an open inline editor (M3) or a running long operation (CL-4)
            dirty = [tab for tab in self.tab_widgets() if _needs_close_answer(tab) or _open_inline_editors(tab)]
            if dirty:
                answers: list[bool] = []
                self._close_chain = True

                def finished(ok: bool) -> None:
                    self._close_chain = False
                    answers.append(bool(ok))
                    if ok and not self._closing and len(answers) == 1 and later:
                        self._close_confirmed = True
                        QTimer.singleShot(0, self, self._close_after_answers)

                later: list[bool] = []
                self._confirm_tabs(dirty, "quit", finished)
                if not answers:
                    later.append(True)     # answered later: close again from finished()
                    event.ignore()
                    return
                if not answers[0]:
                    event.ignore()         # Cancel: the window stays
                    return
                # every tab answered at once (e.g. headless): close now
        self.save_window_state()
        self._closing = True
        # the panels first: they release their results (record sets) before the sessions close
        for key, panel in list(self.panels.items()):
            try:
                if key in DOCK_PANELS:
                    panel.set_session(None)
                fn = getattr(panel, "shutdown", None)
                if callable(fn):
                    fn()
            except Exception:
                log.exception("shutting the %s panel down failed", key)
        self._panel_sessions.clear()
        for tab in self.tab_widgets():
            tab.shutdown()
        self.tabs.blockSignals(True)
        while self.tabs.count():
            widget = self.tabs.widget(0)
            self.tabs.removeTab(0)
            widget.deleteLater()
        self.tabs.blockSignals(False)
        self.status.shutdown()
        try:
            self.app.tasks.detach(self)
            self.app.shutdown(3.0)
        except Exception:
            log.exception("shutting the application context down failed")
        self._close_parentless_windows()
        event.accept()
        self.closed.emit()

    @staticmethod
    def _close_parentless_windows() -> None:
        """Error windows opened without a parent (``report_error(exc)`` of a controller or a task callback) would
        outlive the window -- and keep the application running: they close with it (review L9)."""
        try:
            from jsonreshape.gui.widgets import ErrorDialog, ErrorNotice
        except ImportError:  # pragma: no cover - part of the package
            return
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, (ErrorDialog, ErrorNotice)) and widget.parent() is None and widget.isVisible():
                try:
                    widget.close()
                except RuntimeError:  # pragma: no cover - deleted meanwhile
                    pass
