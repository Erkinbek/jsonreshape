"""One open document in the main window (docs/ARCHITECTURE.md §11.8).

:class:`DocumentTab` = a stack of :class:`~jsonreshape.gui.widgets.InfoBar` messages above
a splitter of two :class:`ViewPane` (default: tree | text). Each pane has a slim view
selector (Daraxt, Jadval, Matn, Xom, Tuzilma) and a :class:`QStackedWidget` that creates its
views lazily -- one instance per kind per pane -- and calls ``activate()`` when a view is
shown. The *active view* is the current view of the pane that last had the keyboard focus;
the main window's global actions (copy, zoom, quick find, F3, expand ...) go to it.

Views come from a *view registry* ``{"tree": TreeView, "table": TableView, ...}`` (classes
called as ``cls(session, parent)``); :func:`default_view_registry` imports the real view
modules of the default panes (the others when first shown: :class:`LazyView`) and leaves
out (and logs) any that cannot be found.

Info bars (each closable; opening/indexing progress is shown by the status bar and the
tasks panel, not here):

* ``open_failed``: the error text + "Qayta urinish" + "Logni ochish";
* ``json_error``: the localized JSON error (line, column, byte offset, reason) + how many
  records before it were opened + "Xatoga o'tish" (raw view at ``doc.error_data_offset``),
  a "open as JSON Lines / concatenated values" suggestion when the reader has a format hint;
* ``broken``: "N ta buzuq qator o'tkazib yuborildi" + "Ro'yxat"
  (:class:`~jsonreshape.gui.navigation.BrokenLinesDialog`);
* ``warnings``: the session's warnings (format, appended data, prepared copies, view) --
  without the ones another bar already says: the automatic record path choice (the
  ``record_path`` bar) and the appended data (the "grown" bar);
* ``record_path``: the record path of wrapped / keyed / single documents with candidates
  + "O'zgartirish…" (:class:`~jsonreshape.gui.navigation.RecordPathDialog`);
* ``file_changed``: changed on disk ("Qayta ochish" / "E'tiborsiz qoldirish") or grown; a
  closed bar stays closed until the file is reopened (a growing log does not bring the
  "grown" bar back every few seconds).

Below the bars a *view banner* tells when the views show a subset or another order of the
records ("Ko'rinish: Filtr: … · 22 / 300 yozuv", with "Hammasini ko'rsatish").

View wiring (duck typing, all optional): ``TextView.open_raw_requested(offset, length)`` →
the other pane shows the raw view at the offset; ``SchemaView.add_column_requested(path)``
→ the tab's table view ``add_column(path)`` (created when needed);
``SchemaView.filter_field_requested(path)`` → :attr:`DocumentTab.filter_field_requested`
(the window's filter panel adds an "exists" condition); ``session.reveal_requested`` →
when no visible pane shows the tree, the pane *not* holding the active view switches to
it (the second pane is shown when needed), so the user's view stays; a view's
``selection_changed`` signal → :attr:`DocumentTab.selection_changed` (status bar).

Lifetime: the tab connects to the session only with its own bound methods (no lambda
holding the tab), and closing the session deletes it (``deleteLater``), so a closed tab
and its views can be freed.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSplitter,
    QStackedWidget,
    QTabBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from jsonreshape.errors import Cancelled, JsonReshapeError
from jsonreshape.gui import theme
from jsonreshape.gui.statusbar import StatusLabel
from jsonreshape.gui.widgets import InfoBar, format_count, format_size, open_log_folder
from jsonreshape.i18n import t

log = logging.getLogger(__name__)

__all__ = ["DocumentTab", "LazyView", "VIEW_KINDS", "VIEW_MODULES", "ViewPane", "default_view_registry",
           "view_label", "BUSY_MARK"]

VIEW_KINDS: tuple[str, ...] = ("tree", "table", "text", "raw", "schema")
VIEW_MODULES: dict[str, tuple[str, str]] = {
    "tree": ("jsonreshape.gui.treeview", "TreeView"),
    "table": ("jsonreshape.gui.tableview", "TableView"),
    "text": ("jsonreshape.gui.textview", "TextView"),
    "raw": ("jsonreshape.gui.rawview", "RawView"),
    "schema": ("jsonreshape.gui.schemaview", "SchemaView"),
}
DEFAULT_PANES = ("tree", "text")
BUSY_MARK = "⟳"     # tab title marker while the document is opened / indexed / filtered
#: Top-to-bottom order of the info bars.
BAR_ORDER = ("open_failed", "json_error", "file_changed", "broken", "record_path", "warnings")
#: Document warnings not shown in the warnings bar because another bar says the same.
APPENDED_WARNING = "reader.warn.appended"
RECORD_PATH_AUTO_WARNING = "reader.record_path_auto"


def _bar_rank(key: str) -> int:
    return BAR_ORDER.index(key) if key in BAR_ORDER else len(BAR_ORDER)


def view_label(kind: str) -> str:
    """Localized name of a view kind (selector buttons, menus)."""
    labels = {
        "tree": t("gui.doctab.view.tree"),
        "table": t("gui.doctab.view.table"),
        "text": t("gui.doctab.view.text"),
        "raw": t("gui.doctab.view.raw"),
        "schema": t("gui.doctab.view.schema"),
    }
    return labels.get(kind, kind)


class LazyView:
    """A view class imported when the first view of its kind is created
    (``LazyView(...)(session, parent)``): the modules of the views a new tab does not show
    are not imported at start-up (R2-22: less GUI-thread work before the window appears)."""

    def __init__(self, module_name: str, class_name: str) -> None:
        self.module_name = module_name
        self.class_name = class_name
        self._cls: type | None = None

    def __repr__(self) -> str:
        return f"<LazyView {self.module_name}.{self.class_name}{'' if self._cls is None else ' (imported)'}>"

    def resolve(self) -> type:
        """The view class (imported now when needed; the import error propagates)."""
        if self._cls is None:
            self._cls = getattr(importlib.import_module(self.module_name), self.class_name)
        return self._cls

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.resolve()(*args, **kwargs)


def default_view_registry() -> dict[str, Any]:
    """``{kind: view class}`` of the real view modules; a module that cannot be found
    is logged and left out (its kind is not offered). The views of the default panes
    (:data:`DEFAULT_PANES`) are imported now, the others when first shown
    (:class:`LazyView`; only their module file is looked up now, no import)."""
    registry: dict[str, Any] = {}
    for kind in VIEW_KINDS:
        module_name, class_name = VIEW_MODULES[kind]
        try:
            if kind in DEFAULT_PANES:
                module = importlib.import_module(module_name)
                registry[kind] = getattr(module, class_name)
                continue
            if importlib.util.find_spec(module_name) is None:
                raise ImportError(f"no module named {module_name!r}")
            registry[kind] = LazyView(module_name, class_name)
        except Exception:
            log.warning("view %r is not available (%s.%s cannot be imported)", kind, module_name, class_name,
                        exc_info=True)
    return registry


# ==================================================================================
# ViewPane
# ==================================================================================
class ViewPane(QWidget):
    """A view selector + a stack of lazily created views (one per kind)."""

    view_changed = Signal(str)        # kind now shown
    view_created = Signal(object)     # a new view instance
    clicked = Signal()                # the selector was used (the pane becomes active)

    def __init__(self, session: Any, *, view_registry: dict[str, type], index: int = 0,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.index = index
        self._registry = dict(view_registry)
        self._views: dict[str, QWidget] = {}
        self._kind: str | None = None
        self._active = False
        self.setObjectName(f"view_pane_{index}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        # the view selector: a slim tab bar (elides / scrolls when the pane is narrow)
        self.selector = QTabBar(self)
        self.selector.setObjectName("view_selector")
        self.selector.setDocumentMode(True)
        self.selector.setExpanding(False)
        self.selector.setUsesScrollButtons(True)
        self.selector.setElideMode(Qt.TextElideMode.ElideRight)
        self.selector.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._tab_kinds: list[str] = []
        self._syncing = True
        for number, kind in enumerate(VIEW_KINDS, start=1):
            if kind not in self._registry:
                continue
            index = self.selector.addTab(view_label(kind))
            self.selector.setTabData(index, kind)
            self.selector.setTabToolTip(index, t("gui.doctab.view.tip", view=view_label(kind), key=f"Ctrl+{number}"))
            self._tab_kinds.append(kind)
        self._syncing = False
        self.selector.setVisible(bool(self._tab_kinds))
        self.selector.tabBarClicked.connect(lambda _index: self.clicked.emit())
        self.selector.currentChanged.connect(self._selector_changed)
        self.empty_label = QLabel(t("gui.doctab.no_views"), self)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setVisible(not self._registry)
        layout.addWidget(self.selector)
        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack, 1)
        layout.addWidget(self.empty_label)
        theme.notifier().theme_changed.connect(self._update_selector_style)

    # -- queries -------------------------------------------------------------------------
    def kinds(self) -> list[str]:
        """View kinds this pane offers (registry order)."""
        return [k for k in VIEW_KINDS if k in self._registry]

    def current_kind(self) -> str | None:
        return self._kind

    def current_view(self) -> QWidget | None:
        return self._views.get(self._kind) if self._kind is not None else None

    def view(self, kind: str) -> QWidget | None:
        """The pane's view of ``kind`` when it was created (else ``None``)."""
        return self._views.get(kind)

    def views(self) -> dict[str, QWidget]:
        return dict(self._views)

    # -- views ----------------------------------------------------------------------------
    def ensure_view(self, kind: str) -> QWidget | None:
        """The view of ``kind`` (created now when needed, not shown); ``None`` when the
        kind is not available or its construction failed (reported)."""
        view = self._views.get(kind)
        if view is not None:
            return view
        cls = self._registry.get(kind)
        if cls is None:
            return None
        try:
            view = cls(self.session, self.stack)
        except Exception as exc:
            log.exception("creating the %s view failed", kind)
            try:
                self.session.app.report_error(exc, self)
            except Exception:
                log.debug("report_error failed", exc_info=True)
            return None
        view.setObjectName(f"view_{kind}_{self.index}")
        self._views[kind] = view
        self.stack.addWidget(view)
        self.view_created.emit(view)
        return view

    def set_view(self, kind: str, *, focus: bool = False) -> QWidget | None:
        """Show the view of ``kind`` (created lazily; ``activate()`` is called)."""
        view = self.ensure_view(kind)
        if view is None:
            return None
        changed = kind != self._kind
        self._kind = kind
        self.stack.setCurrentWidget(view)
        self._sync_selector()
        try:
            view.activate()
        except Exception as exc:
            log.exception("activating the %s view failed", kind)
            try:
                self.session.app.report_error(exc, self)
            except Exception:
                log.debug("report_error failed", exc_info=True)
        if focus:
            self.focus_view()
        if changed:
            self.view_changed.emit(kind)
        return view

    def focus_view(self) -> None:
        """Give the keyboard focus to the current view (its focus proxy, else its first
        focusable child)."""
        view = self.current_view()
        if view is None:
            return
        target = view.focusProxy() or view
        if target.focusPolicy() == Qt.FocusPolicy.NoFocus:
            for child in view.findChildren(QWidget):
                if child.focusPolicy() != Qt.FocusPolicy.NoFocus and not child.isHidden():
                    target = child
                    break
        target.setFocus(Qt.FocusReason.OtherFocusReason)

    def selector_kinds(self) -> list[str]:
        """The kinds in the selector, left to right."""
        return list(self._tab_kinds)

    def _sync_selector(self) -> None:
        if self._kind in self._tab_kinds:
            index = self._tab_kinds.index(self._kind)
            if self.selector.currentIndex() != index:
                self._syncing = True
                try:
                    self.selector.setCurrentIndex(index)
                finally:
                    self._syncing = False
        self._update_selector_style()

    def _selector_changed(self, index: int) -> None:
        if self._syncing or not 0 <= index < len(self._tab_kinds):
            return
        self.set_view(self._tab_kinds[index], focus=True)
        self.clicked.emit()

    # -- active marker ------------------------------------------------------------------------
    def set_active(self, on: bool) -> None:
        if on != self._active:
            self._active = on
            self._update_selector_style()

    def is_active(self) -> bool:
        return self._active

    def _update_selector_style(self) -> None:
        """The active pane's current view is shown in the accent color."""
        current = self.selector.currentIndex() if self._kind is not None else -1
        accent = QColor(theme.color("accent"))
        for index in range(self.selector.count()):
            self.selector.setTabTextColor(index, accent if self._active and index == current else QColor())

    def selector_color(self, kind: str) -> QColor:
        """The text color of ``kind`` in the selector (invalid = the default color)."""
        return self.selector.tabTextColor(self._tab_kinds.index(kind))

    def shutdown(self) -> None:
        """Shut every created view down (tab closing)."""
        for kind, view in list(self._views.items()):
            fn = getattr(view, "shutdown", None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    log.exception("shutting the %s view down failed", kind)


# ==================================================================================
# view banner
# ==================================================================================
class ViewBanner(QFrame):
    """"Ko'rinish: Filtr: … · 22 / 300 yozuv" + "Hammasini ko'rsatish": shown while the
    views show a filtered / sorted / search-result view (not all records in file order).
    Live counts refresh it at most every :attr:`REFRESH_MS`."""

    REFRESH_MS = 300

    def __init__(self, session: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.setObjectName("view_banner")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 4, 2)
        layout.setSpacing(6)
        self.label = StatusLabel(self, mode=Qt.TextElideMode.ElideMiddle, fixed=False)
        self.label.setContentsMargins(0, 0, 0, 0)
        self.label.update_metrics()
        layout.addWidget(self.label, 1)
        self.clear_button = QToolButton(self)
        self.clear_button.setText(t("gui.doctab.view_banner.clear"))
        self.clear_button.setToolTip(t("gui.doctab.view_banner.clear.tip"))
        self.clear_button.setAutoRaise(True)
        self.clear_button.clicked.connect(self.clear_view)
        layout.addWidget(self.clear_button)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self.setVisible(False)
        self._apply_style()
        theme.notifier().theme_changed.connect(self._apply_style)

    def _apply_style(self) -> None:
        accent = QColor(theme.color("accent"))
        self.setStyleSheet(f"QFrame#view_banner {{ border: 1px solid {accent.name()}; border-radius: 3px; }}")

    def schedule(self) -> None:
        """Refresh soon (coalesced: live counts change up to 5 times a second)."""
        if not self._timer.isActive():
            self._timer.start()

    def text(self) -> str:
        """The banner text ("" when all records are shown in file order)."""
        s = self.session
        doc = s.doc
        if doc is None or s.state in ("new", "opening", "closed") or doc.kind == "single" or s.view_is_base:
            return ""
        text = t("gui.doctab.view_banner", label=s.view_label(), shown=format_count(s.view_len()),
                 total=format_count(int(doc.record_count)))
        if s.view_busy:
            text = t("gui.doctab.view_banner.busy", text=text)
        return text

    def refresh(self) -> None:
        self._timer.stop()
        text = self.text()
        self.label.setText(text)
        self.label.set_tip(text)
        if self.isHidden() == bool(text):
            self.setVisible(bool(text))

    def clear_view(self) -> None:
        self.session.reset_view()


# ==================================================================================
# DocumentTab
# ==================================================================================
class DocumentTab(QWidget):
    """One document: info bars + two view panes (see the module docstring)."""

    title_changed = Signal()               # title() / busy state changed
    active_view_changed = Signal()         # the active pane or its view changed
    selection_changed = Signal()           # the active view's selection changed
    filter_field_requested = Signal(str)   # a field the user wants an "exists" filter for
    record_path_dialog_opened = Signal(object)
    broken_lines_dialog_opened = Signal(object)

    def __init__(self, session: Any, *, view_registry: dict[str, type] | None = None,
                 parent: QWidget | None = None, panes: tuple[str, str] = DEFAULT_PANES) -> None:
        super().__init__(parent)
        self.session = session
        self.app = session.app
        self._registry = dict(view_registry) if view_registry is not None else default_view_registry()
        self._closed = False
        self._bars: dict[str, InfoBar] = {}
        self._dismissed: dict[str, Any] = {}
        self._grown_shown = False      # the "grown" bar told about appended data (shown or closed)
        self.pending_ref: Any = None   # a node to reveal once the session opened (main window)
        self._active = 0
        self.setObjectName("document_tab")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.bar_box = QWidget(self)
        self.bar_box.setObjectName("info_bars")
        self._bar_layout = QVBoxLayout(self.bar_box)
        self._bar_layout.setContentsMargins(4, 4, 4, 2)
        self._bar_layout.setSpacing(3)
        self.bar_box.setVisible(False)
        layout.addWidget(self.bar_box)
        self.view_banner = ViewBanner(session, self)
        layout.addWidget(self.view_banner)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setObjectName("view_splitter")
        self.splitter.setChildrenCollapsible(False)
        self.panes: list[ViewPane] = []
        for i in range(2):
            pane = ViewPane(session, view_registry=self._registry, index=i, parent=self.splitter)
            pane.view_changed.connect(lambda _kind, p=i: self._pane_view_changed(p))
            pane.view_created.connect(lambda view, p=i: self._hook_view(p, view))
            pane.clicked.connect(lambda p=i: self.set_active_pane(p))
            self.splitter.addWidget(pane)
            self.panes.append(pane)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        layout.addWidget(self.splitter, 1)

        kinds = self.panes[0].kinds()
        for i, wanted in enumerate(panes[:2]):
            kind = wanted if wanted in kinds else (kinds[min(i, len(kinds) - 1)] if kinds else None)
            if kind is not None:
                self.panes[i].set_view(kind)
        self.panes[0].set_active(True)

        s = session
        s.state_changed.connect(self._on_state)
        s.opened.connect(self._on_opened)
        s.open_failed.connect(self._on_open_failed)
        s.index_finished.connect(self._on_index_finished)
        s.warnings_changed.connect(self._refresh_warnings)
        s.file_changed.connect(self._on_file_changed)
        s.reveal_requested.connect(self._on_reveal)
        s.view_busy_changed.connect(self._on_busy_changed)
        s.view_changed.connect(self.view_banner.refresh)
        s.count_changed.connect(self.view_banner.schedule)
        app = QApplication.instance()
        self._focus_hooked = False
        if app is not None:
            app.focusChanged.connect(self._on_focus_changed)
            self._focus_hooked = True

    def _on_busy_changed(self, _busy: bool) -> None:
        self.title_changed.emit()
        self.view_banner.refresh()

    # ------------------------------------------------------------------ title
    @property
    def busy(self) -> bool:
        s = self.session
        return s.state in ("new", "opening", "indexing") or bool(getattr(s, "view_busy", False))

    @property
    def failed(self) -> bool:
        return self.session.state == "failed"

    def title(self) -> str:
        """The tab text: file name + :data:`BUSY_MARK` while busy."""
        name = self.session.name
        return f"{BUSY_MARK} {name}" if self.busy else name

    def tooltip(self) -> str:
        return os.path.normpath(self.session.path)

    # ------------------------------------------------------------------ panes / views
    def pane(self, index: int) -> ViewPane:
        return self.panes[index]

    def active_pane(self) -> ViewPane:
        return self.panes[self._active]

    def active_pane_index(self) -> int:
        return self._active

    def other_pane(self, pane: ViewPane | int | None = None) -> ViewPane:
        index = pane if isinstance(pane, int) else (pane.index if pane is not None else self._active)
        return self.panes[1 - index]

    def set_active_pane(self, index: int) -> None:
        index = 0 if index not in (0, 1) else index
        if index == 1 and not self.second_pane_visible():
            index = 0
        if index == self._active:
            return
        self._active = index
        for i, pane in enumerate(self.panes):
            pane.set_active(i == index)
        self.active_view_changed.emit()

    def active_view(self) -> QWidget | None:
        """The current view of the active pane."""
        return self.active_pane().current_view()

    def active_kind(self) -> str | None:
        return self.active_pane().current_kind()

    def available_kinds(self) -> list[str]:
        return self.panes[0].kinds()

    def set_active_view_kind(self, kind: str, *, focus: bool = True) -> QWidget | None:
        """Ctrl+1..5: show ``kind`` in the active pane."""
        return self.active_pane().set_view(kind, focus=focus)

    def views(self, kind: str | None = None) -> list[QWidget]:
        """Created views (of ``kind``), the visible panes' first."""
        visible = self._visible_panes()
        ordered = visible + [p for p in self.panes if p not in visible]
        return [view for pane in ordered for k, view in pane.views().items() if kind is None or k == kind]

    def _visible_panes(self) -> list[ViewPane]:
        return [p for p in self.panes if p.index == 0 or self.second_pane_visible()]

    def showing(self, kind: str) -> ViewPane | None:
        """The visible pane whose current view is ``kind``."""
        for pane in self._visible_panes():
            if pane.current_kind() == kind:
                return pane
        return None

    def second_pane_visible(self) -> bool:
        return not self.panes[1].isHidden()

    def set_second_pane_visible(self, on: bool) -> None:
        pane = self.panes[1]
        if on == (not pane.isHidden()):
            return
        pane.setVisible(on)
        if on:
            if pane.current_kind() is None and pane.kinds():
                pane.set_view(DEFAULT_PANES[1] if DEFAULT_PANES[1] in pane.kinds() else pane.kinds()[0])
        elif self._active == 1:
            self.set_active_pane(0)
            return
        self.active_view_changed.emit()

    def orientation(self) -> Qt.Orientation:
        return self.splitter.orientation()

    def set_orientation(self, orientation: Qt.Orientation) -> None:
        self.splitter.setOrientation(orientation)

    def _pane_view_changed(self, index: int) -> None:
        if index == self._active:
            self.active_view_changed.emit()
        self.panes[index]._update_selector_style()

    def _on_focus_changed(self, _old: QWidget | None, now: QWidget | None) -> None:
        if self._closed or now is None:
            return
        for i, pane in enumerate(self.panes):
            if pane is now or pane.isAncestorOf(now):
                self.set_active_pane(i)
                return

    # -- view wiring ------------------------------------------------------------------------
    def _hook_view(self, pane_index: int, view: QWidget) -> None:
        signal = getattr(view, "open_raw_requested", None)
        if signal is not None:
            signal.connect(lambda offset, length=1, p=pane_index: self.show_raw_at(offset, length,
                                                                                   pane=self.other_pane(p)))
        signal = getattr(view, "add_column_requested", None)
        if signal is not None:
            signal.connect(lambda path, p=pane_index: self.add_table_column(path, from_pane=p))
        signal = getattr(view, "filter_field_requested", None)
        if signal is not None:
            signal.connect(lambda path: self.filter_field_requested.emit(str(path)))
        signal = getattr(view, "selection_changed", None)
        if signal is not None and hasattr(signal, "connect"):
            signal.connect(lambda *_args, v=view: self._view_selection_changed(v))

    def _view_selection_changed(self, view: QWidget) -> None:
        if view is self.active_view():
            self.selection_changed.emit()

    def show_raw_at(self, offset: int, length: int = 1, *, pane: ViewPane | None = None) -> QWidget | None:
        """Show the raw view at ``offset`` in ``pane`` (default: the second pane when it is
        visible, else the active one)."""
        if pane is None:
            pane = self.panes[1] if self.second_pane_visible() else self.active_pane()
        if pane.index == 1 and not self.second_pane_visible():
            self.set_second_pane_visible(True)
        view = pane.set_view("raw")
        if view is None:
            return None
        fn = getattr(view, "goto_offset", None)
        if callable(fn):
            try:
                fn(int(offset), max(1, int(length)))
            except Exception as exc:
                log.exception("goto_offset failed")
                self.app.report_error(exc, self)
        return view

    def add_table_column(self, path: str, *, from_pane: int | None = None) -> QWidget | None:
        """Add ``path`` as a column of the tab's table view (created in the other pane
        when there is none) and show it."""
        if "table" not in self._registry:
            return None
        target: ViewPane | None = self.showing("table")
        if target is None:
            for pane in self.panes:
                if pane.view("table") is not None:
                    target = pane
                    break
        if target is None:
            target = self.other_pane(from_pane if from_pane is not None else self._active)
        if target.index == 1 and not self.second_pane_visible():
            self.set_second_pane_visible(True)
        view = target.set_view("table")
        fn = getattr(view, "add_column", None)
        if callable(fn):
            try:
                fn(path)
            except Exception as exc:
                log.exception("add_column failed")
                self.app.report_error(exc, self)
        return view

    def table_view(self) -> QWidget | None:
        """The table view to take export columns from (visible first)."""
        pane = self.showing("table")
        if pane is not None:
            return pane.current_view()
        views = self.views("table")
        return views[0] if views else None

    def _on_reveal(self, _ref: Any) -> None:
        """"Show in tree": when no visible pane shows the tree, the pane that does not
        hold the active view shows it (the second pane is shown first when hidden), so the
        view the user works in stays."""
        if self._closed or "tree" not in self._registry:
            return
        if self.showing("tree") is not None:
            return
        target = self.other_pane(self._active)
        if target.index == 1 and not self.second_pane_visible():
            self.set_second_pane_visible(True)
        target.set_view("tree")

    # ------------------------------------------------------------------ info bars
    def info_bar(self, key: str) -> InfoBar | None:
        bar = self._bars.get(key)
        return bar if bar is not None and not bar.isHidden() else None

    def info_bar_keys(self) -> list[str]:
        return [k for k, bar in self._bars.items() if not bar.isHidden()]

    def _set_bar(self, key: str, kind: str, message: str, *, detail: str | None = None,
                 buttons: list[tuple[str, Callable[[], Any]]] | None = None, signature: Any = None) -> InfoBar | None:
        if self._closed:
            return None
        if signature is not None and self._dismissed.get(key) == signature:
            return None
        self._remove_bar(key)
        bar = InfoBar(kind, message, detail=detail, buttons=buttons or (), parent=self.bar_box)
        bar.setObjectName(f"infobar_{key}")
        bar.closed.connect(lambda k=key, b=bar, sig=signature: self._bar_closed(k, b, sig))
        rank = _bar_rank(key)
        position = sum(1 for k in self._bars if _bar_rank(k) < rank)   # the layout keeps BAR_ORDER
        self._bar_layout.insertWidget(position, bar)
        self._bars[key] = bar
        self.bar_box.setVisible(True)
        return bar

    def _remove_bar(self, key: str) -> None:
        bar = self._bars.pop(key, None)
        if bar is not None:
            self._bar_layout.removeWidget(bar)
            bar.hide()
            bar.deleteLater()
        self.bar_box.setVisible(any(not b.isHidden() for b in self._bars.values()))

    def _bar_closed(self, key: str, bar: InfoBar, signature: Any) -> None:
        if self._bars.get(key) is bar:
            self._dismissed[key] = signature if signature is not None else True
            self._remove_bar(key)

    # -- session events ------------------------------------------------------------------------
    def _on_state(self, state: str) -> None:
        if self._closed:
            return
        if state == "opening":
            for key in ("open_failed", "json_error", "broken", "file_changed", "record_path", "warnings"):
                self._remove_bar(key)
            for key in ("json_error", "broken", "file_changed", "open_failed"):
                self._dismissed.pop(key, None)
            self._grown_shown = False
        self.view_banner.refresh()
        self.title_changed.emit()

    def _on_opened(self) -> None:
        if self._closed:
            return
        self._refresh_record_path()
        self._refresh_warnings()

    def _on_index_finished(self) -> None:
        if self._closed:
            return
        self._refresh_error()
        self._refresh_broken()
        self._refresh_record_path()
        self._refresh_warnings()
        self.title_changed.emit()

    def _on_open_failed(self, exc: BaseException) -> None:
        if self._closed:
            return
        buttons: list[tuple[str, Callable[[], Any]]] = [(t("gui.doctab.retry"), self.reopen)]
        detail = None
        if isinstance(exc, Cancelled):
            message = t("gui.doctab.open_cancelled", name=self.session.name)
            kind = "warning"
        else:
            kind = "error"
            if isinstance(exc, JsonReshapeError):
                message = exc.message
                detail = exc.hint
            else:
                message = t("gui.doctab.open_failed", name=self.session.name,
                            error=f"{type(exc).__name__}: {exc}")
            buttons.append((t("gui.doctab.open_log"), self._open_log))
        if self.session.record_path is not None or self.session.format is not None:
            buttons.append((t("gui.doctab.open_auto"), lambda: self.session.reopen(record_path=None, format=None)))
            buttons.append((t("gui.doctab.record_path.change"), self.open_record_path_dialog))
        self._set_bar("open_failed", kind, message, detail=detail, buttons=buttons)
        self.title_changed.emit()

    def _open_log(self) -> None:
        try:
            open_log_folder()
        except Exception:
            log.exception("could not open the log folder")

    def reopen(self) -> None:
        """F5 / "Qayta urinish": open the file again with the same options."""
        if not self._closed:
            self.session.reopen()

    def _refresh_error(self) -> None:
        doc = self.session.doc
        err = getattr(doc, "error", None) if doc is not None else None
        if err is None:
            self._remove_bar("json_error")
            return
        details = [t("gui.doctab.error.partial", count=format_count(doc.record_count))]
        buttons: list[tuple[str, Callable[[], Any]]] = []
        if doc.error_data_offset is not None and "raw" in self._registry:
            buttons.append((t("gui.doctab.error.goto"), self.go_to_error))
        hint = getattr(doc, "format_hint", None)
        if hint == "jsonl":
            details.append(t("gui.doctab.error.hint_jsonl"))
            buttons.append((t("gui.doctab.error.open_jsonl"), lambda: self.session.reopen(format="jsonl")))
        elif hint == "stream":
            details.append(t("gui.doctab.error.hint_stream"))
            buttons.append((t("gui.doctab.error.open_stream"), lambda: self.session.reopen(format="stream")))
        if getattr(err, "code", "") in ("target_not_found", "target_not_container"):
            buttons.append((t("gui.doctab.record_path.change"), self.open_record_path_dialog))
        signature = ("json_error", err.offset, err.code)
        self._set_bar("json_error", "error", err.message, detail="\n".join(details), buttons=buttons,
                      signature=signature)

    def go_to_error(self) -> QWidget | None:
        """"Xatoga o'tish": the raw view at the JSON error (second pane, or the active one)."""
        doc = self.session.doc
        offset = getattr(doc, "error_data_offset", None) if doc is not None else None
        if offset is None:
            return None
        return self.show_raw_at(offset, 1)

    def _refresh_broken(self) -> None:
        doc = self.session.doc
        count = int(getattr(doc, "broken_count", 0) or 0) if doc is not None else 0
        if not count:
            self._remove_bar("broken")
            return
        self._set_bar("broken", "warning", t("gui.doctab.broken", count=format_count(count)),
                      buttons=[(t("gui.doctab.broken.list"), self.open_broken_lines_dialog)],
                      signature=("broken", count))

    def open_broken_lines_dialog(self) -> QWidget | None:
        if self.session.doc is None:
            return None
        from jsonreshape.gui.navigation import BrokenLinesDialog

        dialog = BrokenLinesDialog(self.session, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.goto_requested.connect(lambda offset, length: self.show_raw_at(offset, length))
        dialog.show()
        self.broken_lines_dialog_opened.emit(dialog)
        return dialog

    def _refresh_warnings(self) -> None:
        try:
            items = self.session.warning_items()
        except Exception:
            log.debug("warning_items failed", exc_info=True)
            items = []
        record_path_bar = self._record_path_message() is not None
        texts = []
        for key, text in items:
            if not text:
                continue
            if key == APPENDED_WARNING and self._grown_shown:
                continue          # the "grown" bar says it (or the user closed it)
            if key == RECORD_PATH_AUTO_WARNING and record_path_bar:
                continue          # the record path bar says it
            texts.append(text)
        if not texts:
            self._remove_bar("warnings")
            self._dismissed.pop("warnings", None)
            return
        detail = "\n".join(texts[1:]) if len(texts) > 1 else None
        self._set_bar("warnings", "warning", texts[0], detail=detail, signature=tuple(texts))

    def _record_path_message(self) -> str | None:
        """The record path bar's message (``None`` when the document needs none)."""
        doc = self.session.doc
        if doc is None:
            return None
        info = doc.info
        kind = info.kind
        if kind not in ("wrapped", "keyed", "single") or not (info.candidates or kind == "wrapped"):
            return None
        how = t("gui.doctab.record_path.auto") if info.auto else t("gui.doctab.record_path.chosen")
        if kind == "wrapped":
            return t("gui.doctab.record_path.wrapped", path=info.record_path_text or "$", how=how)
        if kind == "keyed":
            return t("gui.doctab.record_path.keyed", how=how)
        return t("gui.doctab.record_path.single", how=how)

    def _refresh_record_path(self) -> None:
        doc = self.session.doc
        if doc is None:
            return
        message = self._record_path_message()
        if message is None:
            self._remove_bar("record_path")
            return
        info = doc.info
        kind = info.kind
        detail = None
        if info.candidates:
            detail = t("gui.doctab.record_path.candidates", count=len(info.candidates),
                       paths=", ".join(c.text for c in info.candidates[:5]))
        self._set_bar("record_path", "info", message, detail=detail,
                      buttons=[(t("gui.doctab.record_path.change"), self.open_record_path_dialog)],
                      signature=("record_path", kind, info.record_path_text))

    def open_record_path_dialog(self) -> QWidget:
        from jsonreshape.gui.navigation import RecordPathDialog

        dialog = RecordPathDialog(self.session, self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.open()
        self.record_path_dialog_opened.emit(dialog)
        return dialog

    def _on_file_changed(self, kind: str) -> None:
        if self._closed:
            return
        if kind == "changed":
            self._set_bar("file_changed", "warning", t("gui.doctab.changed"), detail=t("gui.doctab.changed.detail"),
                          buttons=[(t("gui.doctab.reopen"), self.reopen),
                                   (t("gui.doctab.ignore"), lambda: self._dismiss("file_changed"))],
                          signature="changed")
        else:
            doc = self.session.doc
            grown = int(getattr(doc, "appended_bytes", 0) or 0) if doc is not None else 0
            bar = self.info_bar("file_changed")
            if bar is not None and self._bars.get("file_changed") is bar and bar.kind() == "info":
                bar.set_message(t("gui.doctab.grown", size=format_size(grown)))   # still open: new size
            else:
                # once closed, it stays closed while the file keeps growing (until a reopen)
                self._set_bar("file_changed", "info", t("gui.doctab.grown", size=format_size(grown)),
                              buttons=[(t("gui.doctab.reopen"), self.reopen)], signature="grown")
            self._grown_shown = True
            self._refresh_warnings()

    def _dismiss(self, key: str) -> None:
        bar = self._bars.get(key)
        if bar is not None:
            bar.close_bar()

    # ------------------------------------------------------------------ lifetime
    @property
    def closed(self) -> bool:
        return self._closed

    def shutdown(self) -> None:
        """Tab closing: shut the views down, close the session (nothing calls back)."""
        if self._closed:
            return
        self._closed = True
        if self._focus_hooked:
            try:
                QApplication.instance().focusChanged.disconnect(self._on_focus_changed)
            except (RuntimeError, TypeError, AttributeError):
                pass
            self._focus_hooked = False
        for pane in self.panes:
            pane.shutdown()
        try:
            self.app.tasks.detach(self)
        except Exception:
            log.debug("detach failed", exc_info=True)
        try:
            self.session.close()
        except Exception:
            log.exception("closing the session failed")
