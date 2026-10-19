"""One open document in the main window (docs/ARCHITECTURE.md §11.8).

:class:`DocumentTab` = a stack of :class:`~jsonreshape.gui.widgets.InfoBar` messages above
a splitter of two :class:`ViewPane` (default: tree | text). Each pane has a slim view
selector (Daraxt, Jadval, Matn, Xom, Tuzilma, Tuzilma quruvchi -- Ctrl+1..6) and a
:class:`QStackedWidget` that creates its views lazily -- one instance per kind per pane -- and
calls ``activate()`` when a view is shown. The *active view* is the current view of the pane
that last had the keyboard focus; the main window's global actions (copy, zoom, quick find,
F3, expand ...) go to it. The structure builder (``mapping``, docs/ARCHITECTURE.md §13.5)
keeps one designer per session (:func:`jsonreshape.gui.mapmodel.state_for`), so its two
possible instances (one per pane) show the same tree.

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
* ``file_changed``: changed on disk ("Qayta ochish" / "E'tiborsiz qoldirish"; with unsaved
  changes also "O'zgarishlarni JSON Patch sifatida saqlash…", written without reading the
  changed file: ``export_patch(path, diff=False)``) or grown; a closed bar stays closed
  until the file is reopened (a growing log does not bring the "grown" bar back every few
  seconds).

Editing (docs/ARCHITECTURE.md §12.9) adds three bars:

* ``recovery`` (from the edit controller's ``pending_found``, sent again after every action
  with what is left): unsaved changes of an earlier run -- "Tiklash" / "O'chirish…" (asks)
  / "JSON Patch sifatida saqlash…"; a layer of an older version of the file or a damaged one
  offers "JSON Patch sifatida saqlash…" / "O'chirish…" (asks); a layer another tab / program
  holds is information only, without a step count (``index.json`` counts steps only when a
  layer is closed), and while this tab cannot edit because of it the read-only bar alone says
  so (one bar); an interrupted save (journal) offers "Saqlashni yakunlash" (``roll_pending``)
  first. When a roll leaves the journal (a conflict, or an error) the bar shows that it could
  neither be finished nor undone, the files involved and "Saqlashni yakunlash" (try again) /
  "JSON Patch sifatida saqlash…" / "O'chirish…" (asks). One layer is shown at a time (the next
  one after it was handled). The bar of a layer of this file version that nobody else holds
  cannot be closed (editing stays off until it is answered); the others can be put aside
  (``ignore_pending``).
* A file that cannot be opened because it is missing (``error.file_not_found``) may be the
  victim of an interrupted save whose fallback stopped between its two renames: a silent task
  looks for its journal by path (:func:`jsonreshape.saver.find_journals`); when there is one,
  the recovery bar offers "Saqlashni yakunlash" (:func:`jsonreshape.saver.roll_journal_at` in
  a task, then the file is opened again) and, after a conflict, the JSON Patch export
  (:func:`jsonreshape.saver.layer_store_at` + ``jsonpatch.export_layer``) and the deletion
  (``editstore.delete_layer``, asks first) of that layer.
* Leftovers of interrupted saves next to the file (``.<name>.<hex>.jrsave`` / ``.old``) are
  removed by :func:`jsonreshape.saver.sweep_leftovers` in a silent task once per opened
  document and after a journal roll or a restore, while this tab's controller holds the file's
  lock and no interrupted save waits; the status bar says how much space was freed.
* ``view_outdated`` (``session.view_outdated``): a filtered / sorted view too big to be
  remapped after a structural edit shows all records; "Yangilash" computes it again.
* ``readonly``: why editing is off (the controller's ``reason``) when no other bar says it
  (``locked``, ``huge_single``, ``partial``, ``unavailable``, ...); for a file that grew the
  "grown" bar carries the reason as its detail.

The tab title starts with :data:`DIRTY_MARK` while the document has unsaved changes.
Every way of dropping the document's edits from memory asks first (non-blocking, through
the controller's ``ask_close``): :meth:`DocumentTab.confirm_close` (the main window's tab /
window close), :meth:`DocumentTab.reopen` (F5, "Qayta urinish", the bars' reopen buttons;
the record path dialog uses :func:`confirm_drop_edits` itself). After the answer the
session is reopened with ``discard_edits=True`` (the answer already decided: saved,
discarded, or -- headless without an answer handler -- the layer kept for recovery).

Below the bars a *view banner* tells when the views show a subset or another order of the
records ("Ko'rinish: Filtr: … · 22 / 300 yozuv", with "Hammasini ko'rsatish").

Stage 5 (docs/ARCHITECTURE.md §14.9): the ``json_error`` bar (a syntax error, not a missing record
path), the ``broken`` bar and -- for a brace list (``{...}, {...}``: ``reader.warn_brace_list``) -- the
``warnings`` bar offer "Ta'mirlash…" / "Standart JSON'ga aylantirish…": :attr:`DocumentTab.repair_requested`
``(path, hints)`` with the format / record path the tab was opened with (:meth:`DocumentTab.repair_hints`);
the main window opens the repair window (a repaired copy; the file of the tab is never changed). The schema
view's ``stats_requested(field)`` / ``dupes_requested(field)`` are forwarded as the tab's
:attr:`~DocumentTab.stats_requested` / :attr:`~DocumentTab.dupes_requested` (the window's statistics dock
and duplicates dialog of this document).

Fix round 3 leftovers (review CL-4, M3): :func:`confirm_drop_edits` also asks a clean document whose edit
controller runs a long operation (``needs_close_answer``: "wait / stop / cancel"), and -- when asked from
the tab itself or a dialog of it (its reopen buttons, F5 through :meth:`DocumentTab.reopen`, the record
path dialog) -- first commits the inline editors open in the tab (bar buttons take no focus, so focus-out
never commits them): the question comes once their steps were applied; an invalid value refuses (the
status bar says why, the editor stays open, nothing is reopened).

View wiring (duck typing, all optional): ``TextView.open_raw_requested(offset, length)`` →
the other pane shows the raw view at the offset; ``SchemaView.add_column_requested(path)``
→ the tab's table view ``add_column(path)`` (created when needed);
``SchemaView.filter_field_requested(path)`` → :attr:`DocumentTab.filter_field_requested`
(the window's filter panel adds an "exists" condition); ``SchemaView.mapping_requested`` →
:meth:`DocumentTab.show_mapping` (the structure builder in that pane, or the visible pane
showing it already); a view's ``history_changed`` (the structure builder's own undo / redo)
→ :attr:`DocumentTab.view_state_changed` when it is the active view (the main window's undo /
redo follow it); ``session.reveal_requested`` →
when no visible pane shows the tree, the pane *not* holding the active view switches to
it (the second pane is shown when needed), so the user's view stays; a view's
``selection_changed`` signal → :attr:`DocumentTab.selection_changed` (status bar).

Lifetime: the tab connects to the session (and its edit controller, which the session
creates when the index is complete and replaces on every reopen: re-hooked on
``index_finished`` / ``state_changed`` / ``data_changed``) only with its own bound
methods (no lambda holding the tab), and closing the session deletes it
(``deleteLater``), so a closed tab and its views can be freed.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import os
import time
from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
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
           "view_label", "BRACE_LIST_WARNING", "BUSY_MARK", "DIRTY_MARK", "accepts_keyword", "confirm_drop_edits",
           "reopen_session", "session_edit"]

VIEW_KINDS: tuple[str, ...] = ("tree", "table", "text", "raw", "schema", "mapping")
VIEW_MODULES: dict[str, tuple[str, str]] = {
    "tree": ("jsonreshape.gui.treeview", "TreeView"),
    "table": ("jsonreshape.gui.tableview", "TableView"),
    "text": ("jsonreshape.gui.textview", "TextView"),
    "raw": ("jsonreshape.gui.rawview", "RawView"),
    "schema": ("jsonreshape.gui.schemaview", "SchemaView"),
    "mapping": ("jsonreshape.gui.mapview", "MapView"),
}
DEFAULT_PANES = ("tree", "text")
BUSY_MARK = "⟳"     # tab title marker while the document is opened / indexed / filtered
DIRTY_MARK = "*"    # tab / window title marker while the document has unsaved changes
#: Top-to-bottom order of the info bars.
BAR_ORDER = ("open_failed", "json_error", "recovery", "file_changed", "view_outdated", "readonly", "broken",
             "record_path", "warnings")
#: Document warnings not shown in the warnings bar because another bar says the same.
APPENDED_WARNING = "reader.warn.appended"
RECORD_PATH_AUTO_WARNING = "reader.record_path_auto"
#: The reader's warning of a brace list (``{...}, {...}``): its bar offers the repair window (stage 5).
BRACE_LIST_WARNING = "reader.warn_brace_list"
#: Controller reasons without a "read-only" bar: passing states, or another bar says it (the
#: file-changed bar; the "grown" bar carries the reason as its detail).
QUIET_REASONS = frozenset({"indexing", "saving", "applying", "closed", "recovery_pending", "file_changed",
                           "file_grown"})
#: Session methods that compute an outdated view again (the first one present is used).
REFRESH_VIEW_METHODS = ("refresh_view", "refresh_outdated_view", "reapply_view")
#: Display order of the pending layers in the recovery bar.
_PENDING_ORDER = ("journal", "matching", "damaged", "stale", "locked")


def _bar_rank(key: str) -> int:
    return BAR_ORDER.index(key) if key in BAR_ORDER else len(BAR_ORDER)


def accepts_keyword(fn: Any, name: str) -> bool:
    """``fn`` takes the keyword argument ``name`` (or ``**kwargs``)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def session_edit(session: Any) -> Any:
    """The session's edit controller (``None``: none yet, or a session without editing)."""
    if session is None or getattr(session, "closed", False):
        return None
    return getattr(session, "edit", None)


def _tab_of_widget(widget: Any) -> DocumentTab | None:
    """The document tab ``widget`` belongs to: the tab itself, a widget inside it or a dialog parented to it
    (the record path dialog); ``None`` for anything else (the main window)."""
    w = widget
    while w is not None:
        if isinstance(w, DocumentTab):
            return w
        try:
            w = w.parentWidget()
        except (AttributeError, RuntimeError):     # not a widget / deleted
            return None
    return None


def confirm_drop_edits(session: Any, parent: QWidget | None, on_done: Callable[[bool], Any], *,
                       why: str = "close") -> None:
    """Ask before the unsaved changes of ``session`` leave memory (``why``: ``close`` |
    ``reopen`` | ``quit``) -- non-blocking, through the controller's ``ask_close`` (Save /
    Don't save / Cancel; the controller adapts the buttons) -- also while the controller runs a long
    operation of a clean document (``needs_close_answer``: "wait / stop / cancel", review CL-4).
    ``on_done(answer)`` is called exactly once: at once (``True``) when there is nothing to ask, else
    when answered (it may be called before this function returns).

    When ``parent`` is the session's tab (or lies inside it: the tab's own reopen buttons, the record
    path dialog), the inline editors open in the tab are committed first (review M3,
    :func:`jsonreshape.gui.editing.settle_edits`): the question comes once their steps were applied, and
    an editor holding an invalid value answers ``False`` at once (the status bar says why; the editor stays
    open). The main window settles its tabs itself (``parent`` is the window then)."""
    called: list[bool] = []

    def done(ok: Any) -> None:
        if called:
            return
        called.append(bool(ok))
        on_done(bool(ok))

    def ask() -> None:
        edit = session_edit(session)
        if edit is None or not (bool(getattr(edit, "dirty", False))
                                or bool(getattr(edit, "needs_close_answer", False))):
            done(True)
            return
        ask_close = getattr(edit, "ask_close", None)
        if not callable(ask_close):
            done(True)
            return
        try:
            ask_close(parent, done, why=why)
        except Exception as exc:
            log.exception("asking about the unsaved changes failed")
            app = getattr(session, "app", None)
            if app is not None:
                app.report_error(exc, parent)
            done(False)

    tab = _tab_of_widget(parent)
    if tab is None or tab.closed or tab.session is not session:
        ask()
        return
    from jsonreshape.gui.editing import settle_edits

    app = getattr(session, "app", None)
    if settle_edits(tab, session_edit(session), ask, views=tab.views(),
                    show_status=getattr(app, "show_status", None)) is False:
        done(False)          # an editor refused (an invalid value, a key check on its way): nothing asked


def reopen_session(session: Any, **options: Any) -> None:
    """``session.reopen(**options)`` once the user answered about its unsaved changes
    (``discard_edits=True`` when the session takes it: whatever the answer was -- saved,
    discarded, or the layer kept for recovery -- the reopen goes on)."""
    if session is None or getattr(session, "closed", False):
        return
    fn = session.reopen
    kwargs = {"discard_edits": True, **options}
    # keywords of later stages (discard_edits, keep_view) only when the session takes them
    kwargs = {k: v for k, v in kwargs.items() if k in ("record_path", "format", "use_cache") or accepts_keyword(fn, k)}
    fn(**kwargs)


def _pending_kind(info: Any) -> str:
    """``locked`` | ``journal`` | ``damaged`` | ``stale`` | ``matching``."""
    if getattr(info, "locked", False):
        return "locked"
    if getattr(info, "saving", None):
        return "journal"
    if getattr(info, "damaged", None):
        return "damaged"
    if not getattr(info, "matches", True):
        return "stale"
    return "matching"


def _when(ts: Any) -> str:
    try:
        if not ts or float(ts) <= 0:        # a layer held elsewhere may have no times in index.json
            return "?"
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def _journal_files(journal: Any) -> str:
    """The files a save journal names (temp file, backup, the backup's and the original's copies)."""
    if not isinstance(journal, dict):
        return "-"
    names = [str(journal[k]) for k in ("temp", "backup", "old_backup", "aside") if journal.get(k)]
    return ", ".join(names) or "-"


def _not_found(exc: BaseException) -> bool:
    """The open failed because the file is not there (``error.file_not_found``)."""
    return getattr(exc, "key", None) == "error.file_not_found"


def view_label(kind: str) -> str:
    """Localized name of a view kind (selector buttons, menus)."""
    labels = {
        "tree": t("gui.doctab.view.tree"),
        "table": t("gui.doctab.view.table"),
        "text": t("gui.doctab.view.text"),
        "raw": t("gui.doctab.view.raw"),
        "schema": t("gui.doctab.view.schema"),
        "mapping": t("gui.doctab.view.mapping"),
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

    title_changed = Signal()               # title() / busy state / unsaved-changes mark changed
    active_view_changed = Signal()         # the active pane or its view changed
    selection_changed = Signal()           # the active view's selection changed
    filter_field_requested = Signal(str)   # a field the user wants an "exists" filter for
    record_path_dialog_opened = Signal(object)
    broken_lines_dialog_opened = Signal(object)
    edit_state_changed = Signal()          # the edit controller (or its state: dirty, busy, undo...) changed
    view_state_changed = Signal()          # the active view's own undo / redo state changed (structure builder)
    repair_requested = Signal(str, dict)   # "Ta'mirlash…" of a bar: (file path, hints {format, record_path})
    stats_requested = Signal(str)          # the schema view's "Statistika": a field text
    dupes_requested = Signal(str)          # the schema view's "Takroriy yozuvlar…": a field text

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
        # editing (§12.9)
        self._edit: Any = None             # the hooked edit controller
        self._edit_conns: list[tuple[Any, Any]] = []
        self._last_dirty = False
        self._file_state: str | None = None    # "changed" while the file-changed bar says so
        self._pending: list[Any] = []          # PendingInfo of the recovery bar
        self._readonly_reason: str | None = None   # the reason the read-only bar shows
        # interrupted saves (journals): rolls in flight / rolls that left their journal (conflict)
        self._rolls_running: dict[str, Any] = {}   # layer key -> the journal a roll was asked for
        self._roll_tried: dict[str, Any] = {}      # layer key -> the journal a roll left in place
        self._roll_texts: dict[str, str] = {}      # layer key -> the roll's own (conflict) message
        self._path_journals: list[Any] = []        # journals found by path for a missing file
        self._journal_task: Any = None
        self._folded_locked = False                # locked layers left to the read-only bar (L4)
        self._sweep_wanted = True                  # remove leftovers of interrupted saves (once per open)
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
        s.view_changed.connect(self._refresh_view_outdated)
        s.count_changed.connect(self.view_banner.schedule)
        # editing signals of the session (§12.9; optional: a session without editing has none)
        for name, slot in (("data_changed", self._on_data_changed), ("dirty_changed", self._on_dirty_changed),
                           ("view_outdated_changed", self._refresh_view_outdated),
                           ("edit_changed", self._sync_edit), ("path_changed", self._on_path_changed)):
            signal = getattr(s, name, None)
            if signal is not None and hasattr(signal, "connect"):
                signal.connect(slot)
        app = QApplication.instance()
        self._focus_hooked = False
        if app is not None:
            app.focusChanged.connect(self._on_focus_changed)
            self._focus_hooked = True
        self._sync_edit()

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

    @property
    def edit(self) -> Any:
        """The session's edit controller (``None`` while there is none)."""
        return session_edit(self.session) if not self._closed else None

    @property
    def dirty(self) -> bool:
        """The document has unsaved changes (the session's applied state or its controller)."""
        if self._closed:
            return False
        if getattr(self.session, "dirty", False) is True:
            return True
        edit = self.edit
        return edit is not None and bool(getattr(edit, "dirty", False))

    def display_name(self) -> str:
        """The file name, with :data:`DIRTY_MARK` in front while there are unsaved changes."""
        name = self.session.name
        return f"{DIRTY_MARK}{name}" if self.dirty else name

    def title(self) -> str:
        """The tab text: :meth:`display_name` + :data:`BUSY_MARK` in front while busy."""
        name = self.display_name()
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
        """Ctrl+1..6: show ``kind`` in the active pane."""
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
        signal = getattr(view, "mapping_requested", None)
        if signal is not None and hasattr(signal, "connect"):
            signal.connect(lambda *_args, p=pane_index: self.show_mapping(from_pane=p))
        # stage 5 (the schema view): "Statistika" / double-click and "Takroriy yozuvlar…" -> the window
        signal = getattr(view, "stats_requested", None)
        if signal is not None and hasattr(signal, "connect"):
            signal.connect(lambda field="", *_args: self._forward_field(self.stats_requested, field))
        signal = getattr(view, "dupes_requested", None)
        if signal is not None and hasattr(signal, "connect"):
            signal.connect(lambda field="", *_args: self._forward_field(self.dupes_requested, field))
        signal = getattr(view, "history_changed", None)
        if signal is not None and hasattr(signal, "connect"):
            signal.connect(lambda *_args, v=view: self._view_history_changed(v))

    def _forward_field(self, signal: Any, field: Any) -> None:
        """A view asked for the statistics / duplicates of ``field``: the tab's signal (the main window
        shows the statistics dock / the duplicates dialog of this tab's document)."""
        if not self._closed:
            signal.emit(str(field or ""))

    def _view_selection_changed(self, view: QWidget) -> None:
        if view is self.active_view():
            self.selection_changed.emit()

    def _view_history_changed(self, view: QWidget) -> None:
        if not self._closed and view is self.active_view():
            self.view_state_changed.emit()

    def show_mapping(self, *, from_pane: int | None = None) -> QWidget | None:
        """"Tuzilma quruvchi…" (the schema view's action): the structure builder, in a visible
        pane that shows it already, else in the pane the request came from (default: the
        active one); it gets the focus."""
        if self._closed or "mapping" not in self._registry:
            return None
        pane = self.showing("mapping")
        if pane is None:
            index = from_pane if from_pane in (0, 1) else self._active
            pane = self.panes[index]
            if pane.index == 1 and not self.second_pane_visible():
                self.set_second_pane_visible(True)
        view = pane.set_view("mapping", focus=True)
        self.set_active_pane(pane.index)
        return view

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
                 buttons: list[tuple[str, Callable[[], Any]]] | None = None, signature: Any = None,
                 closable: bool = True) -> InfoBar | None:
        if self._closed:
            return None
        if signature is not None and self._dismissed.get(key) == signature:
            return None
        self._remove_bar(key)
        bar = InfoBar(kind, message, detail=detail, buttons=buttons or (), closable=closable, parent=self.bar_box)
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
            for key in ("open_failed", "json_error", "broken", "file_changed", "record_path", "warnings",
                        "recovery", "readonly", "view_outdated"):
                self._remove_bar(key)
            for key in ("json_error", "broken", "file_changed", "open_failed", "recovery", "readonly"):
                self._dismissed.pop(key, None)
            self._grown_shown = False
            self._file_state = None
            self._pending = []
            self._readonly_reason = None
            self._rolls_running.clear()
            self._roll_tried.clear()
            self._roll_texts.clear()
            self._path_journals = []
            self._sweep_wanted = True
        self.view_banner.refresh()
        self._sync_edit()
        self.title_changed.emit()

    def _on_opened(self) -> None:
        if self._closed:
            return
        self._refresh_record_path()
        self._refresh_warnings()
        self._sync_edit()

    def _on_index_finished(self) -> None:
        if self._closed:
            return
        self._refresh_error()
        self._refresh_broken()
        self._refresh_record_path()
        self._refresh_warnings()
        self._sync_edit()
        # the session may create its edit controller after this signal: look once more
        QTimer.singleShot(0, self, self._sync_edit)
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
            buttons.append((t("gui.doctab.open_auto"), lambda: self.reopen(record_path=None, format=None)))
            buttons.append((t("gui.doctab.record_path.change"), self.open_record_path_dialog))
        self._set_bar("open_failed", kind, message, detail=detail, buttons=buttons)
        self._sync_edit()
        self.title_changed.emit()
        if _not_found(exc):
            self._look_for_journals()

    def _open_log(self) -> None:
        try:
            open_log_folder()
        except Exception:
            log.exception("could not open the log folder")

    def confirm_close(self, on_done: Callable[[bool], Any], *, why: str = "close",
                      parent: QWidget | None = None) -> None:
        """Ask about the unsaved changes before they leave memory (see
        :func:`confirm_drop_edits`); ``on_done(True)`` at once when there are none."""
        if self._closed:
            on_done(True)
            return
        confirm_drop_edits(self.session, parent if parent is not None else self, on_done, why=why)

    def reopen(self, on_done: Callable[[bool], Any] | None = None, **options: Any) -> None:
        """F5 / "Qayta urinish" / the bars' reopen buttons: open the file again (``options``:
        ``record_path`` / ``format``; omitted = the same). What is being typed in an inline editor
        of the tab is committed first (bar buttons take no focus, so nothing commits it by focus-out:
        review M3; an invalid value stops here), then unsaved changes -- or a long operation of the
        controller (CL-4) -- are asked about (non-blocking); ``on_done(reopened)`` is called once
        answered (:func:`confirm_drop_edits`)."""
        if self._closed:
            if on_done is not None:
                on_done(False)
            return

        def answered(ok: bool) -> None:
            go = ok and not self._closed and not getattr(self.session, "closed", False)
            if go:
                try:
                    reopen_session(self.session, **options)
                except Exception as exc:
                    log.exception("reopening %s failed", self.session.name)
                    self.app.report_error(exc, self)
                    go = False
            if on_done is not None:
                on_done(go)

        self.confirm_close(answered, why="reopen")

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
            buttons.append((t("gui.doctab.error.open_jsonl"), lambda: self.reopen(format="jsonl")))
        elif hint == "stream":
            details.append(t("gui.doctab.error.hint_stream"))
            buttons.append((t("gui.doctab.error.open_stream"), lambda: self.reopen(format="stream")))
        if getattr(err, "code", "") in ("target_not_found", "target_not_container"):
            buttons.append((t("gui.doctab.record_path.change"), self.open_record_path_dialog))
        else:
            # a syntax error: a repaired copy can be made (never in place; the repair window shows the report)
            buttons.append((t("gui.repairdialog.bar.repair"), self.request_repair))
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

    def repair_hints(self) -> dict[str, Any]:
        """What the tab was opened with, for the repair window: ``format`` / ``record_path`` (``None`` values
        left out)."""
        s = self.session
        hints = {"format": getattr(s, "format", None), "record_path": getattr(s, "record_path", None)}
        return {key: value for key, value in hints.items() if value is not None}

    def request_repair(self) -> None:
        """"Ta'mirlash…" of the JSON-error and broken-lines bars, "Standart JSON'ga aylantirish…" of the brace-list
        warning: :attr:`repair_requested` ``(path, hints)`` -- the main window opens the repair window of the file
        (a repaired COPY is written; the file of this tab is never changed)."""
        if not self._closed:
            self.repair_requested.emit(str(self.session.path), self.repair_hints())

    def _refresh_broken(self) -> None:
        doc = self.session.doc
        count = int(getattr(doc, "broken_count", 0) or 0) if doc is not None else 0
        if not count:
            self._remove_bar("broken")
            return
        self._set_bar("broken", "warning", t("gui.doctab.broken", count=format_count(count)),
                      buttons=[(t("gui.doctab.broken.list"), self.open_broken_lines_dialog),
                               (t("gui.repairdialog.bar.repair"), self.request_repair)],
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
        brace_list = False
        for key, text in items:
            if not text:
                continue
            if key == APPENDED_WARNING and self._grown_shown:
                continue          # the "grown" bar says it (or the user closed it)
            if key == RECORD_PATH_AUTO_WARNING and record_path_bar:
                continue          # the record path bar says it
            brace_list = brace_list or key == BRACE_LIST_WARNING
            texts.append(text)
        if not texts:
            self._remove_bar("warnings")
            self._dismissed.pop("warnings", None)
            return
        detail = "\n".join(texts[1:]) if len(texts) > 1 else None
        # a brace list ({...}, {...}: not standard JSON) can be converted by the repair window (stage 5)
        buttons = [(t("gui.repairdialog.bar.brace_list"), self.request_repair)] if brace_list else None
        self._set_bar("warnings", "warning", texts[0], detail=detail, buttons=buttons, signature=tuple(texts))

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
            self._file_state = "changed"
            self._show_changed_bar()
        else:
            doc = self.session.doc
            grown = int(getattr(doc, "appended_bytes", 0) or 0) if doc is not None else 0
            bar = self.info_bar("file_changed")
            if bar is not None and self._bars.get("file_changed") is bar and bar.kind() == "info":
                bar.set_message(t("gui.doctab.grown", size=format_size(grown)))   # still open: new size
            else:
                # once closed, it stays closed while the file keeps growing (until a reopen)
                self._set_bar("file_changed", "info", t("gui.doctab.grown", size=format_size(grown)),
                              detail=self._grown_detail(),
                              buttons=[(t("gui.doctab.reopen"), self.reopen)], signature="grown")
            self._grown_shown = True
            self._refresh_warnings()

    def _grown_detail(self) -> str | None:
        """The "grown" bar's detail: why editing is off when the controller says it is the
        growth (the bar says it instead of a separate read-only bar)."""
        edit = self._edit
        if edit is None or bool(getattr(edit, "available", True)) or getattr(edit, "reason", "") != "file_grown":
            return None
        from jsonreshape.gui.actions import edit_reason_text

        return edit_reason_text(edit)

    def _refresh_grown_detail(self) -> None:
        bar = self.info_bar("file_changed")
        if bar is None or bar.kind() != "info":
            return
        want = self._grown_detail()
        if (bar.detail() or None) != want:
            bar.set_detail(want)

    def _show_changed_bar(self) -> None:
        """The "changed on disk" bar; with unsaved changes it also offers to save them as a
        JSON Patch (written from the edit layer alone: the file changed)."""
        dirty = self.dirty
        buttons: list[tuple[str, Callable[[], Any]]] = [(t("gui.doctab.reopen"), self.reopen)]
        if dirty:
            buttons.append((t("gui.doctab.changed.save_patch"), self.save_changes_as_patch))
        buttons.append((t("gui.doctab.ignore"), lambda: self._dismiss("file_changed")))
        detail = t("gui.doctab.changed.detail_dirty") if dirty else t("gui.doctab.changed.detail")
        self._set_bar("file_changed", "warning", t("gui.doctab.changed"), detail=detail, buttons=buttons,
                      signature="changed")

    def _dismiss(self, key: str) -> None:
        bar = self._bars.get(key)
        if bar is not None:
            bar.close_bar()

    # ------------------------------------------------------------------ editing (§12.9)
    def _sync_edit(self, *_args: Any) -> None:
        """Follow the session's edit controller (created when the index is complete,
        replaced by every reopen)."""
        if self._closed:
            return
        edit = session_edit(self.session)
        if edit is self._edit:
            self._on_edit_state()
            return
        self._unhook_edit()
        self._edit = edit
        if edit is not None:
            for name, slot in (("state_changed", self._on_edit_state), ("pending_found", self._on_pending_found)):
                signal = getattr(edit, name, None)
                if signal is not None and hasattr(signal, "connect"):
                    signal.connect(slot)
                    self._edit_conns.append((signal, slot))
            known = getattr(edit, "pending", None)
            if isinstance(known, (list, tuple)) and known:
                self._on_pending_found(list(known))
        self._on_edit_state()

    def _unhook_edit(self) -> None:
        for signal, slot in self._edit_conns:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):   # the controller is gone already
                pass
        self._edit_conns.clear()
        self._edit = None

    def _on_data_changed(self, *_args: Any) -> None:
        self._sync_edit()

    def _on_path_changed(self, *_args: Any) -> None:
        """Save As: the session shows another file now (title, tooltip)."""
        if not self._closed:
            self.title_changed.emit()

    def _on_dirty_changed(self, *_args: Any) -> None:
        self._sync_edit()

    def _on_edit_state(self, *_args: Any) -> None:
        """The controller's state changed (dirty, busy, available, undo...)."""
        if self._closed:
            return
        dirty = self.dirty
        if dirty != self._last_dirty:
            self._last_dirty = dirty
            self.title_changed.emit()
            if self._file_state == "changed" and self.info_bar("file_changed") is not None:
                self._show_changed_bar()
        self._refresh_readonly()
        if self._fold_locked() != self._folded_locked:
            self._render_recovery()         # the read-only bar now says it (or no longer does)
        self._refresh_grown_detail()
        self._maybe_sweep()                 # e.g. the roll that sent the pending layers has ended now
        self.edit_state_changed.emit()

    def _refresh_readonly(self) -> None:
        """The "read-only" note: why editing is off, when no other bar says it."""
        edit = self._edit
        reason = ""
        if edit is not None and not bool(getattr(edit, "available", True)):
            reason = str(getattr(edit, "reason", "") or "")
        if not reason or reason in QUIET_REASONS:
            self._readonly_reason = None
            self._remove_bar("readonly")
            return
        if reason == self._readonly_reason and self._bars.get("readonly") is not None:
            return                       # shown already (or closed by the user): no flicker
        from jsonreshape.gui.actions import edit_reason_text

        self._readonly_reason = reason
        self._set_bar("readonly", "info", t("gui.doctab.readonly"), detail=edit_reason_text(edit),
                      signature=("readonly", reason))

    # -- recovery bar ------------------------------------------------------------------------
    def pending(self) -> list[Any]:
        """The pending layers the recovery bar knows (``PendingInfo``): the controller's, or --
        while the file cannot be opened because it is missing -- the interrupted saves found by
        its path."""
        if self._path_journals and self.session.state == "failed":
            return list(self._path_journals)
        return list(self._pending)

    def _on_pending_found(self, infos: Any) -> None:
        """The controller's list of pending layers (sent again after every restore / discard
        / export / journal roll: the bar shows what is left)."""
        if self._closed:
            return
        self._pending = [p for p in (infos or []) if p is not None]
        # the answer to a roll: a journal still there after it could neither be finished nor undone
        for key, journal in list(self._rolls_running.items()):
            if any(str(getattr(p, "key", "")) == key and getattr(p, "saving", None) == journal
                   for p in self._pending):
                self._roll_tried[key] = journal
            self._rolls_running.pop(key, None)
        self._render_recovery()
        self._maybe_sweep()

    def _fold_locked(self) -> bool:
        """This tab cannot edit because another tab / program edits the file: the read-only bar says
        it, so layers held elsewhere get no bar of their own (one bar, review L4)."""
        edit = self._edit
        return (edit is not None and not bool(getattr(edit, "available", True))
                and getattr(edit, "reason", "") == "locked")

    def _recovery_infos(self) -> list[Any]:
        if self._path_journals and self.session.state == "failed":
            infos = list(self._path_journals)
        else:
            infos = list(self._pending)
            if self._fold_locked():
                infos = [p for p in infos if not getattr(p, "locked", False)]
        return sorted(infos, key=lambda p: _PENDING_ORDER.index(_pending_kind(p)))

    def _render_recovery(self) -> None:
        self._folded_locked = self._fold_locked()
        infos = self._recovery_infos()
        if not infos:
            self._remove_bar("recovery")
            return
        info = infos[0]
        kind = _pending_kind(info)
        key = str(getattr(info, "key", ""))
        when = _when(getattr(info, "updated", None) or getattr(info, "created", None))
        steps = format_count(int(getattr(info, "steps", 0) or 0))
        label = str(getattr(info, "label", "") or "")
        patch = (t("gui.doctab.recovery.save_patch"), lambda i=info: self.export_pending(i))
        delete = (t("gui.doctab.recovery.delete"), lambda i=info: self.discard_pending(i))
        finish = (t("gui.doctab.recovery.journal.finish"), lambda i=info: self.resolve_journal(i))
        # a layer of this file version not held elsewhere must be answered before editing; the
        # others (older versions, held elsewhere) may be put aside (``ignore_pending``)
        closable = not (getattr(info, "matches", False) and not getattr(info, "locked", False))
        variant = kind
        if kind == "journal":
            journal = getattr(info, "saving", None)
            if key in self._roll_tried and self._roll_tried[key] == journal:
                # a roll left the journal: say so and offer what can be done with the changes
                variant = "journal_conflict"
                bar_kind = "error"
                message = t("gui.doctab.recovery.journal_conflict", time=when)
                reason = self._roll_texts.get(key) or t("gui.doctab.recovery.journal_conflict.files",
                                                        files=_journal_files(journal))
                detail = t("gui.doctab.recovery.journal_conflict.detail", reason=reason)
                buttons = [finish, patch, delete]
            elif self._is_path_journal(info):
                bar_kind = "warning"
                message = t("gui.doctab.recovery.journal_missing", time=when)
                detail = t("gui.doctab.recovery.journal_missing.detail")
                buttons = [finish]
            else:
                bar_kind = "warning"
                message = t("gui.doctab.recovery.journal", time=when)
                detail = t("gui.doctab.recovery.journal.detail")
                buttons = [finish]
        elif kind == "matching":
            bar_kind = "warning"
            message = t("gui.doctab.recovery.found", time=when, steps=steps)
            detail = t("gui.doctab.recovery.found.detail", label=label) if label \
                else t("gui.doctab.recovery.found.detail_nolabel")
            buttons = [(t("gui.doctab.recovery.restore"), lambda i=info: self.restore_pending(i)),
                       (t("gui.doctab.recovery.discard"), lambda i=info: self.discard_pending(i)), patch]
        elif kind == "damaged":
            bar_kind = "error"
            message = t("gui.doctab.recovery.damaged", time=when, steps=steps)
            detail = t("gui.doctab.recovery.damaged.detail", reason=str(getattr(info, "damaged", "") or ""))
            buttons = [patch, delete]
        elif kind == "stale":
            bar_kind = "warning"
            message = t("gui.doctab.recovery.stale", time=when, steps=steps)
            detail = t("gui.doctab.recovery.stale.detail")
            buttons = [patch, delete]
        else:   # locked: index.json counts the steps of a layer only when it is closed -- no count
            bar_kind = "info"
            message = t("gui.doctab.recovery.locked", time=when)
            detail = t("gui.doctab.recovery.locked.detail")
            buttons = []
        if len(infos) > 1:
            detail = f"{detail}\n{t('gui.doctab.recovery.more', count=len(infos) - 1)}"
        signature = ("recovery", variant, key, int(getattr(info, "steps", 0) or 0)) if closable else None
        bar = self._set_bar("recovery", bar_kind, message, detail=detail, buttons=buttons, closable=closable,
                            signature=signature)
        if bar is not None and closable:
            bar.closed.connect(lambda i=info: self._put_aside(i))
        elif bar is None and closable and len(infos) > 1:
            # this one was put aside before: show the next one
            if self._is_path_journal(info):
                self._path_journals = [p for p in self._path_journals if p is not info]
            else:
                self._pending = [p for p in self._pending if p is not info]
            self._render_recovery()

    def _put_aside(self, info: Any) -> None:
        """The user closed the bar of a layer that does not block editing: the controller
        hides it until the next open (``ignore_pending``), the next one is shown."""
        if self._closed:
            return
        if self._is_path_journal(info):
            self._path_journals = [p for p in self._path_journals if p is not info]
            self._render_recovery()
            return
        self._pending = [p for p in self._pending if p is not info]
        fn = getattr(self.edit, "ignore_pending", None)
        if callable(fn):
            try:
                fn(info)             # emits pending_found with what is left
                return
            except Exception:
                log.debug("ignore_pending failed", exc_info=True)
        self._render_recovery()

    def _edit_call(self, method: str, *args: Any, **kwargs: Any) -> bool:
        """``edit.<method>(*args, **kwargs)`` with errors reported; False without a controller /
        method."""
        edit = self.edit
        fn = getattr(edit, method, None) if edit is not None else None
        if not callable(fn):
            self.app.show_status(t("gui.doctab.edit_unavailable"), 6000)
            return False
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            log.exception("%s failed", method)
            self.app.report_error(exc, self)
            return False
        return True

    def restore_pending(self, info: Any) -> bool:
        """"Tiklash": load the pending layer's changes."""
        self._sweep_wanted = True
        return self._edit_call("restore_pending", info)

    def discard_pending(self, info: Any) -> bool:
        """"O'chirish…": delete the pending layer (the controller asks; a
        layer found by the path of a missing file: this tab asks)."""
        if self._is_path_journal(info):
            return self._delete_path_layer(info)
        return self._edit_call("discard_pending", info, self)

    def export_pending(self, info: Any, path: str | None = None) -> bool:
        """"JSON Patch sifatida saqlash…": write the pending layer's changes as a JSON Patch."""
        if path is None:
            path = self.ask_save_path(t("gui.doctab.patch.title"), self._default_patch_path(),
                                      t("gui.doctab.patch.filter"))
        if not path:
            return False
        if self._is_path_journal(info):
            return self._export_path_layer(info, os.path.abspath(path))
        return self._edit_call("export_pending", info, os.path.abspath(path))

    def resolve_journal(self, info: Any) -> bool:
        """"Saqlashni yakunlash": finish or undo an interrupted save (the controller's
        ``roll_pending``; its ``restore_pending`` rolls the journal first too; a journal found by
        the path of a missing file: :func:`jsonreshape.saver.roll_journal_at`). When the roll
        leaves the journal, the bar offers the changes' export and deletion."""
        self._sweep_wanted = True
        key = str(getattr(info, "key", ""))
        journal = getattr(info, "saving", None)
        if self._is_path_journal(info):
            return self._finish_path_journal(info)
        edit = self.edit
        method = next((m for m in ("roll_pending", "roll_journal", "restore_pending")
                       if callable(getattr(edit, m, None))), "roll_pending")
        fn = getattr(edit, method, None)
        self._rolls_running[key] = journal
        if not callable(fn) or not accepts_keyword(fn, "on_done"):
            ok = self._edit_call(method, info)
            if not ok:
                self._rolls_running.pop(key, None)
            return ok
        calling = [True]

        def done(result: Any) -> None:
            if calling[0]:                  # refused at once (busy, locked...): nothing ran
                self._rolls_running.pop(key, None)
                return
            self._roll_finished(key, journal, result)

        ok = self._edit_call(method, info, on_done=done)
        calling[0] = False
        if not ok:
            self._rolls_running.pop(key, None)
        return ok

    def _roll_finished(self, key: str, journal: Any, result: Any) -> None:
        """The controller's roll ended (``on_done``): a conflict -- or an error, the journal still
        there -- shows the conflict bar with the roll's own message when there is one."""
        if self._closed:
            return
        self._rolls_running.pop(key, None)
        action = getattr(result, "action", None)
        still = any(str(getattr(p, "key", "")) == key and getattr(p, "saving", None) == journal
                    for p in self._pending)
        if action == "conflict" or (action is None and still):
            self._roll_tried[key] = journal
            if action == "conflict":
                self._roll_texts[key] = str(result)
            self._render_recovery()

    # -- interrupted saves of a missing file (journals found by path) --------------------------------
    def _is_path_journal(self, info: Any) -> bool:
        return any(p is info for p in self._path_journals)

    def _look_for_journals(self) -> None:
        """The file is missing: look for an interrupted save of it (silent task)."""
        if self._closed:
            return
        path = self.session.path
        settings = getattr(self.app, "settings", None)

        def work(_ctx: Any) -> list[Any]:
            from jsonreshape import saver

            return saver.find_journals(path, settings=settings)

        self._journal_task = self.app.tasks.run(work, title=t("gui.doctab.task.journals"), kind="journal",
                                                owner=self, silent=True, on_done=self._journals_found,
                                                on_error=self._journal_lookup_failed)

    def _journals_found(self, infos: Any) -> None:
        if self._closed or self.session.state != "failed":
            return
        self._path_journals = [p for p in (infos or []) if p is not None]
        self._render_recovery()

    def _journal_lookup_failed(self, exc: BaseException) -> None:
        log.warning("looking for an interrupted save of %s failed: %s", self.session.path, exc)

    def _finish_path_journal(self, info: Any) -> bool:
        """Roll the journal of a missing file (task), then open the file again."""
        settings = getattr(self.app, "settings", None)
        key = str(getattr(info, "key", ""))
        journal = getattr(info, "saving", None)

        def work(_ctx: Any) -> Any:
            from jsonreshape import saver

            return saver.roll_journal_at(info, settings=settings)

        def done(result: Any) -> None:
            if self._closed:
                return
            text = str(result)
            if getattr(result, "action", "") == "conflict":
                self.app.show_status(text, 15000)
                self._roll_tried[key] = journal
                self._roll_texts[key] = text
                self._render_recovery()
                return
            self.app.show_status(text, 15000)
            self._path_journals = []
            self._render_recovery()
            self.reopen()

        def failed(exc: BaseException) -> None:
            if self._closed:
                return
            self.app.report_error(exc, self)
            self._roll_tried[key] = journal
            self._render_recovery()

        self.app.tasks.run(work, title=t("gui.doctab.task.roll", name=self.session.name), kind="journal", owner=self,
                           on_done=done, on_error=failed)
        return True

    def _export_path_layer(self, info: Any, target: str) -> bool:
        """The changes of a layer whose file is missing as a JSON Patch (task; written from the
        layer alone)."""
        settings = getattr(self.app, "settings", None)
        notes: list[str] = []

        def work(ctx: Any) -> int:
            from jsonreshape import jsonpatch, saver

            store = saver.layer_store_at(info, settings=settings)
            try:
                return int(jsonpatch.export_layer(store, target, progress=ctx.progress, cancel=ctx.cancel,
                                                  warnings=notes))
            finally:
                store.close(close_reader=True)

        def done(count: int) -> None:
            if not self._closed:
                text = t("gui.doctab.recovery.exported", count=count, path=os.path.normpath(target))
                self.app.show_status(" ".join([text] + notes), 10000)

        def failed(exc: BaseException) -> None:
            if not self._closed and not isinstance(exc, Cancelled):
                self.app.report_error(exc, self)

        self.app.tasks.run(work, title=t("gui.doctab.task.export", name=self.session.name), kind="export",
                           owner=self, on_done=done, on_error=failed)
        return True

    def _delete_path_layer(self, info: Any) -> bool:
        """Delete a layer whose file is missing (asks first; task), then look again."""
        from jsonreshape.gui import widgets

        when = _when(getattr(info, "updated", None) or getattr(info, "created", None))
        steps = format_count(int(getattr(info, "steps", 0) or 0))

        def answer(yes: bool) -> None:
            if not yes or self._closed:
                return

            def work(_ctx: Any) -> bool:
                from jsonreshape.editstore import delete_layer

                return bool(delete_layer(info.dir, key=info.key))

            def done(_ok: bool) -> None:
                if self._closed:
                    return
                self._path_journals = [p for p in self._path_journals if p is not info]
                self._render_recovery()
                self._look_for_journals()

            def failed(exc: BaseException) -> None:
                if not self._closed:
                    self.app.report_error(exc, self)

            self.app.tasks.run(work, title=t("gui.doctab.task.delete", name=self.session.name), kind="journal",
                               owner=self, silent=True, on_done=done, on_error=failed)

        widgets.ask(self, t("gui.doctab.recovery.delete_ask", time=when, steps=steps), answer)
        return True

    # -- leftovers of interrupted saves (review S8) ----------------------------------------------
    def _maybe_sweep(self) -> None:
        """Remove the leftovers of interrupted saves next to the file
        (:func:`jsonreshape.saver.sweep_leftovers`) in a silent task -- once per opened document and
        after a journal roll / restore -- while this tab's controller holds the file's lock (its
        layer key known, editing available or waiting for a recovery answer) and no interrupted
        save or layer held elsewhere is pending."""
        if self._closed or not self._sweep_wanted:
            return
        edit = self._edit
        session = self.session
        own_key = getattr(edit, "layer_key", None) if edit is not None else None
        if not isinstance(own_key, str) or not own_key or session.state != "ready":
            return
        if getattr(edit, "reason", "") not in ("", "recovery_pending") or bool(getattr(edit, "busy", False)):
            return
        if any(getattr(p, "saving", None) or getattr(p, "locked", False) for p in self._pending):
            return
        self._sweep_wanted = False
        path = session.path
        settings = getattr(self.app, "settings", None)

        def work(_ctx: Any) -> Any:
            from jsonreshape import saver

            return saver.sweep_leftovers(path, own_key=own_key, own_file_lock=True, settings=settings)

        self.app.tasks.run(work, title=t("gui.doctab.task.sweep"), kind="journal", owner=self, silent=True,
                           on_done=self._swept, on_error=self._sweep_failed)

    def _swept(self, result: Any) -> None:
        text = result.message() if hasattr(result, "message") else ""
        if text and not self._closed:
            self.app.show_status(text, 10000)

    def _sweep_failed(self, exc: BaseException) -> None:
        log.warning("removing leftovers of interrupted saves next to %s failed: %s", self.session.path, exc)

    def save_changes_as_patch(self, path: str | None = None) -> bool:
        """"O'zgarishlarni JSON Patch sifatida saqlash…" (the file changed on disk): the
        unsaved changes as a JSON Patch built from the edit layer alone (``diff=False``)."""
        edit = self.edit
        fn = getattr(edit, "export_patch", None) if edit is not None else None
        if not callable(fn):
            self.app.show_status(t("gui.doctab.edit_unavailable"), 6000)
            return False
        if path is None:
            path = self.ask_save_path(t("gui.doctab.patch.title"), self._default_patch_path(),
                                      t("gui.doctab.patch.filter"))
        if not path:
            return False
        try:
            if accepts_keyword(fn, "diff"):
                fn(os.path.abspath(path), diff=False)
            else:
                fn(os.path.abspath(path))
        except Exception as exc:
            log.exception("export_patch failed")
            self.app.report_error(exc, self)
            return False
        return True

    def _default_patch_path(self) -> str:
        folder = os.path.dirname(self.session.path)
        stem = os.path.splitext(self.session.name)[0] or "changes"
        if stem.lower().endswith(".json"):
            stem = stem[:-5]
        return os.path.join(folder, f"{stem}.patch.json")

    def ask_save_path(self, title: str, default: str, filters: str) -> str | None:
        """A save-file dialog (tests replace this method); ``None`` when cancelled."""
        path, _selected = QFileDialog.getSaveFileName(self, title, default, filters)
        return path or None

    # -- outdated view -----------------------------------------------------------------------
    def _refresh_view_outdated(self, *_args: Any) -> None:
        if self._closed:
            return
        if not bool(getattr(self.session, "view_outdated", False)):
            self._remove_bar("view_outdated")
            return
        if self.info_bar("view_outdated") is not None:
            return
        self._set_bar("view_outdated", "warning", t("gui.doctab.view_outdated"),
                      detail=t("gui.doctab.view_outdated.detail"),
                      buttons=[(t("gui.doctab.view_outdated.refresh"), self.refresh_view)], closable=False)

    def refresh_view(self) -> bool:
        """"Yangilash": compute the outdated view (filter / sort) again."""
        for name in REFRESH_VIEW_METHODS:
            fn = getattr(self.session, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception as exc:
                    log.exception("%s failed", name)
                    self.app.report_error(exc, self)
                    return False
                return True
        return False

    # ------------------------------------------------------------------ lifetime
    @property
    def closed(self) -> bool:
        return self._closed

    def shutdown(self) -> None:
        """Tab closing: shut the views down, close the session (nothing calls back)."""
        if self._closed:
            return
        self._closed = True
        self._unhook_edit()
        self._pending = []
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
