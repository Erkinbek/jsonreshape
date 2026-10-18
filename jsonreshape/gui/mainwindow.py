"""The main window (docs/ARCHITECTURE.md §11.8; AGENT_PROMPT §4.6, §5, §11).

:class:`MainWindow` holds the document tabs (:class:`~jsonreshape.gui.doctab.DocumentTab`,
closable, movable; the title is the file name plus a busy marker while it is opened /
indexed, the tooltip the full path), the menus (Fayl, Tahrir, Ko'rinish, Navigatsiya,
Qidiruv, Asboblar, Yordam), a toolbar with the most used commands, the docks (search,
filter, tasks; object names ``dock_search`` / ``dock_filter`` / ``dock_tasks`` so
``saveState()`` / ``restoreState()`` keep their places) and the
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
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
from typing import Any, Callable

from PySide6.QtCore import QByteArray, QObject, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QFont, QFontDatabase, QKeySequence
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
from jsonreshape.gui.doctab import VIEW_KINDS, DocumentTab, default_view_registry, view_label
from jsonreshape.gui.guistate import norm_path
from jsonreshape.gui.statusbar import StatusBar
from jsonreshape.gui.taskspanel import TasksPanel
from jsonreshape.gui.widgets import keep_alive, mono_font, open_log_folder, set_clipboard_text
from jsonreshape.i18n import t

log = logging.getLogger(__name__)

__all__ = ["MainWindow", "PANEL_MODULES", "default_panel_factories", "factory_available", "parse_offset"]

PANEL_MODULES: dict[str, tuple[str, str]] = {
    "search": ("jsonreshape.gui.searchpanel", "SearchPanel"),
    "filter": ("jsonreshape.gui.filterpanel", "FilterPanel"),
    "sort": ("jsonreshape.gui.sortdialog", "SortDialog"),
    "export": ("jsonreshape.gui.exportdialog", "ExportDialog"),
    "settings": ("jsonreshape.gui.settingsdialog", "SettingsDialog"),
}
DOCK_PANELS = ("search", "filter")
OPEN_PATTERNS = "*.json *.jsonl *.ndjson *.json.gz *.geojson"
#: GUI state keys of this module (besides ``geometry`` / ``window_state`` / ``last_dir``).
STATE_SPLIT_VERTICAL = "split_vertical"
STATE_SECOND_PANE = "second_pane"


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
        tab.filter_field_requested.connect(self.add_filter_condition)
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
        bar = self.tabs.tabBar()
        if tab.failed:
            bar.setTabTextColor(index, QColor(theme.color("danger")))
        else:
            bar.setTabTextColor(index, QColor())
        if tab is self.current_tab():
            self._update_title()
        self._update_actions()

    def close_tab(self, index: int | None = None) -> None:
        """Close the tab at ``index`` (default: the current one): its views are shut down
        and its session closed."""
        if index is None:
            index = self.tabs.currentIndex()
        tab = self.tabs.widget(index) if index is not None and index >= 0 else None
        if not isinstance(tab, DocumentTab):
            return
        self._recent_origin.discard(id(tab))
        session = tab.session
        for key in DOCK_PANELS:
            if self._panel_sessions.get(key) is session:
                self._set_panel_session(key, None)
        exports = self._running_exports(session)
        self.tabs.removeTab(index)
        tab.pending_ref = None
        tab.shutdown()              # closes the session (deleted later with its connections)
        tab.deleteLater()
        # forget the dialogs of the closed document (they would keep its session alive)
        for key, dialog in list(self.dialogs.items()):
            if getattr(dialog, "session", None) is session:
                del self.dialogs[key]
        if exports:
            # the export goes on with the document it started with; its report still appears
            self.app.show_status(t("gui.main.export_continues", count=exports, name=session.name), 8000)
        self.tab_closed.emit(tab)
        if self.tabs.count() == 0:
            self._on_current_changed(-1)

    def _running_exports(self, session: Any = None) -> int:
        """Exports still running (of ``session``'s file)."""
        runner = getattr(self.app, "_export_runner", None)
        jobs = getattr(runner, "jobs", None)
        if not callable(jobs):
            return 0
        try:
            return len(jobs(session))
        except RuntimeError:  # pragma: no cover - the runner is gone
            return 0

    def close_all_tabs(self) -> None:
        while self.tabs.count():
            self.close_tab(self.tabs.count() - 1)

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
            self.setWindowTitle(t("gui.main.window_title", name=tab.session.name))

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
        areas = {"search": Qt.DockWidgetArea.BottomDockWidgetArea, "filter": Qt.DockWidgetArea.RightDockWidgetArea}
        titles = {"search": t("gui.main.dock.search"), "filter": t("gui.main.dock.filter")}
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
            self._add_dock(key, titles[key], panel, areas[key])

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

    def _create_actions(self) -> None:
        sp = QStyle.StandardPixmap
        # -- file
        self._act("open", t("gui.main.act.open"), self.open_dialog, shortcut=QKeySequence.StandardKey.Open,
                  icon=sp.SP_DialogOpenButton, tip=t("gui.main.act.open.tip"))
        self._act("reopen", t("gui.main.act.reopen"), self.reopen_current, shortcut="F5", icon=sp.SP_BrowserReload,
                  tip=t("gui.main.act.reopen.tip"))
        self._act("record_path", t("gui.main.act.record_path"), self.choose_record_path)
        self._act("export", t("gui.main.act.export"), self.show_export, shortcut="Ctrl+E",
                  icon=sp.SP_DialogSaveButton, tip=t("gui.main.act.export.tip"))
        self._act("close_tab", t("gui.main.act.close_tab"), self.close_tab, shortcut=["Ctrl+W", "Ctrl+F4"])
        self._act("quit", t("gui.main.act.quit"), self.close, shortcut="Ctrl+Q")
        # -- edit
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
        self._act("bookmark_add", t("gui.main.act.bookmark_add"), self.add_bookmark, shortcut="Ctrl+D")
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
        # -- tools / help
        self._act("settings", t("gui.main.act.settings"), self.show_settings)
        self._act("open_logs", t("gui.main.act.open_logs"), self.open_logs)
        self._act("environment", t("gui.main.act.environment"), self.show_environment)
        self._act("about", t("gui.main.act.about"), self.show_about)
        # dock toggles
        for key, title in (("tasks", t("gui.main.dock.tasks")), ("search", t("gui.main.dock.search")),
                           ("filter", t("gui.main.dock.filter"))):
            dock = self.docks.get(key)
            if dock is not None:
                toggle = dock.toggleViewAction()
                toggle.setText(title)
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
        file_menu.addAction(a["export"])
        file_menu.addSeparator()
        file_menu.addAction(a["close_tab"])
        file_menu.addAction(a["quit"])

        edit = menu("edit", t("gui.main.menu.edit"))
        edit.addAction(a["copy_value"])
        edit.addAction(a["copy_path"])
        more = menu("copy_more", t("gui.main.menu.copy_more"), edit)
        from jsonreshape.gui.actions import add_copy_actions

        for key, action in add_copy_actions(more, self.active_view).items():
            self.acts[f"copy_more_{key}"] = action

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
        for key in ("tasks", "search", "filter"):
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
        for key in ("settings", "open_logs", "environment"):
            tools.addAction(a[key])

        help_menu = menu("help", t("gui.main.menu.help"))
        help_menu.addAction(a["about"])

    def _create_toolbar(self) -> None:
        bar = QToolBar(t("gui.main.toolbar"), self)
        bar.setObjectName("main_toolbar")
        bar.setMovable(True)
        a = self.acts
        for key in ("open", "reopen", "export", None, "back", "forward", "goto", None, "search", "filter", "sort",
                    "clear_view", None, "second_pane"):
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
        tab = self.current_tab()
        if tab is not None:
            tab.reopen()

    def choose_record_path(self) -> Any:
        tab = self.current_tab()
        if tab is None:
            return None
        dialog = tab.open_record_path_dialog()
        self.dialogs["record_path"] = dialog
        return dialog

    def show_view_kind(self, kind: str) -> Any:
        """Ctrl+1..5: show ``kind`` in the active pane."""
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

    def show_export(self) -> Any:
        tab = self.current_tab()
        if tab is None or tab.session.doc is None:
            return None
        columns = None
        table = tab.table_view()
        fn = getattr(table, "visible_column_paths", None)
        if callable(fn):
            try:
                columns = list(fn()) or None
            except Exception:
                log.exception("visible_column_paths failed")
        return self._show_dialog("export", tab.session, self, columns=columns, scope="view")

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
        """Ctrl+D: bookmark the current node (name asked; default = its location)."""
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

    def closeEvent(self, event: Any) -> None:  # noqa: N802 (Qt name)
        """Save the window state, close the tabs (sessions), cancel the background tasks
        (waiting briefly) and save the GUI state."""
        if self._closing:
            event.accept()
            return
        exports = self._running_exports()
        if exports and not getattr(self.app, "headless", False) \
                and not self._ask_yes_no(t("gui.main.exports_running.title"),
                                         t("gui.main.exports_running", count=exports)):
            event.ignore()
            return
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
        event.accept()
        self.closed.emit()
