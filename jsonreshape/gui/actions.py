"""The node context menu shared by the views (docs/ARCHITECTURE.md §11.7; AGENT_PROMPT §5).

:func:`node_menu` builds, for one :class:`~jsonreshape.gui.session.NodeRef`:

* **Nusxalash** (copy) submenu: the value as JSON (pretty, ``Ctrl+C`` shown) / compact JSON
  / Python / CSV row / CSV with header, the absolute path in the five styles (dotted with
  ``Ctrl+Shift+C`` shown; each item previews the path), the key;
* **Shu qiymat bo'yicha filtr** (record refs only; the value is loaded in a silent task,
  containers only up to :data:`FILTER_MAX_BYTES`) -> ``session.filter_by_value``;
* **Shu maydon bo'yicha tartiblash** ascending / descending (record refs with a non-empty
  path) -> ``session.sort_by_field``;
* the view's ``extra`` items (e.g. the tree's expand operations);
* **To'liq ko'rish** (:class:`~jsonreshape.gui.widgets.FullValueDialog`), **Daraxtda
  ko'rsatish** (``session.select(ref, origin=view, reveal=True)``), **Yangi tabda ochish**
  (``open_requested`` with ``ref`` / ``record_path`` / ``format``).

Items that do not apply are disabled (never hidden) with a tooltip saying why. Every
action has an ``objectName`` (``copy_value_json``, ``copy_path_dotted``, ``copy_key``,
``filter_by_value``, ``sort_asc``, ``sort_desc``, ``full_value``, ``show_in_tree``,
``open_new_tab``) so callers and tests can find it. The menu does no I/O while it is
built, except ``session.absolute_path(ref)`` (no data read for refs inside records).

:func:`add_copy_actions` gives the main window's Edit menu the same copy commands acting
on the active view.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QMenu, QWidget

from jsonreshape.errors import UserInputError
from jsonreshape.gui import copyfmt
from jsonreshape.gui.widgets import (
    FullValueDialog,
    copy_node_key,
    copy_node_path,
    copy_node_value,
    format_size,
    keep_alive,
    run_value_task,
)
from jsonreshape.i18n import t

log = logging.getLogger(__name__)

__all__ = ["FILTER_MAX_BYTES", "PATH_PREVIEW_CHARS", "add_copy_actions", "node_menu", "popup_node_menu",
           "show_full_value"]

#: "Filter by this value" accepts an object/array only when its JSON is at most this big.
FILTER_MAX_BYTES = 4096
#: Paths shown in the copy-path items are elided to this many characters.
PATH_PREVIEW_CHARS = 60

COPY_VALUE_SHORTCUT = QKeySequence.StandardKey.Copy
COPY_PATH_SHORTCUT = "Ctrl+Shift+C"

ExtraItem = Any   # QAction | QMenu | None (separator) | (text, callback[, enabled[, tooltip]])


def _elide(text: str, limit: int = PATH_PREVIEW_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = (limit - 1) // 2
    tail = limit - 1 - head
    return text[:head] + copyfmt.ELLIPSIS + text[len(text) - tail:]


def _menu_text(text: str) -> str:
    """Menu labels treat ``&`` as a mnemonic marker: keep data text literal."""
    return copyfmt.escape_surrogates(text).replace("&", "&&")


def _disable(action: QAction, reason: str) -> None:
    action.setEnabled(False)
    action.setToolTip(reason)


def _is_container_kind(node: Any) -> bool:
    return getattr(node, "kind", None) in ("object", "array")


def show_full_value(parent: QWidget | None, session: Any, ref: Any) -> FullValueDialog:
    """Open the non-modal "full value" window of ``ref`` (deleted when closed)."""
    owner = parent.window() if parent is not None else None
    dialog = FullValueDialog(session, ref, owner)
    keep_alive(dialog)   # deleted when closed, even without a parent
    dialog.show()
    return dialog


def _filter_by_value(session: Any, ref: Any, parent: QWidget | None, owner: Any) -> Any:
    def check(node: Any) -> None:
        if _is_container_kind(node) and node.size > FILTER_MAX_BYTES:
            raise UserInputError("gui.actions.filter_too_large", size=format_size(node.size))

    def compute(value: Any, cancel: Any) -> Any:
        return value

    def apply(value: Any) -> None:
        session.filter_by_value(ref, value)

    return run_value_task(session, ref, compute, apply, owner=owner, parent=parent,
                          title=t("gui.actions.task.filter"), purpose="filter", check=check)


def _open_new_tab(session: Any, ref: Any) -> None:
    options: dict[str, Any] = {"ref": ref}
    for name in ("record_path", "format"):
        value = getattr(session, name, None)
        if value is not None:
            options[name] = value
    app = session.app
    request = getattr(app, "request_open", None)
    if callable(request):
        request(str(session.path), options)
    else:
        app.open_requested.emit(str(session.path), options)


def _add_extra(menu: QMenu, items: Iterable[ExtraItem]) -> None:
    for item in items:
        if item is None:
            menu.addSeparator()
        elif isinstance(item, QMenu):
            menu.addMenu(item)
        elif isinstance(item, QAction):
            menu.addAction(item)
        else:
            text, callback, *rest = item
            action = menu.addAction(str(text))
            action.triggered.connect(lambda _checked=False, cb=callback: cb())
            if rest and not rest[0]:
                _disable(action, str(rest[1]) if len(rest) > 1 and rest[1] else "")
            elif len(rest) > 1 and rest[1]:
                action.setToolTip(str(rest[1]))


def node_menu(parent: QWidget, session: Any, ref: Any, *, view: Any = None,
              extra: Iterable[ExtraItem] | None = None, node: Any = None) -> QMenu:
    """The context menu of ``ref`` (see the module doc). ``view``: the view asking (the
    ``origin`` of "show in tree"; a tree disables that item); ``extra``: items inserted
    after the filter/sort section; ``node``: optional already-known node info (any object
    with ``kind`` and ``size``) used to disable "filter" for big containers at once. The
    caller owns the returned menu (see :func:`popup_node_menu`)."""
    menu = QMenu(parent)
    menu.setObjectName("node_menu")
    menu.setToolTipsVisible(True)
    owner = view if view is not None else session
    try:
        abs_path: tuple | None = tuple(session.absolute_path(ref))
    except Exception:
        log.debug("absolute_path(%r) failed", ref, exc_info=True)
        abs_path = None

    # -- copy ------------------------------------------------------------------------------
    copy_menu = menu.addMenu(t("gui.actions.copy"))
    copy_menu.setObjectName("copy_menu")
    copy_menu.setToolTipsVisible(True)
    for fmt in copyfmt.COPY_FORMATS:
        action = copy_menu.addAction(t("gui.actions.copy_value", format=copyfmt.format_label(fmt)))
        action.setObjectName(f"copy_value_{fmt}")
        action.triggered.connect(lambda _checked=False, f=fmt: copy_node_value(session, ref, f, owner=owner,
                                                                               parent=parent))
        if fmt == "json":
            _show_shortcut(action, QKeySequence(COPY_VALUE_SHORTCUT))
    copy_menu.addSeparator()
    for style in copyfmt.PATH_STYLES:
        if abs_path is None:
            preview = "?"
        else:
            text = copyfmt.path_text(abs_path, style)
            preview = _elide(text) if text else t("gui.actions.root")
        action = copy_menu.addAction(_menu_text(t("gui.actions.copy_path", style=copyfmt.style_label(style),
                                                  path=preview)))
        action.setObjectName(f"copy_path_{style}")
        action.triggered.connect(lambda _checked=False, s=style: copy_node_path(session, ref, s))
        if style == "dotted":
            _show_shortcut(action, QKeySequence(COPY_PATH_SHORTCUT))
        if abs_path is None:
            _disable(action, t("gui.actions.no_path"))
    copy_menu.addSeparator()
    key = copyfmt.key_text(abs_path) if abs_path else None
    key_action = copy_menu.addAction(_menu_text(t("gui.actions.copy_key", key=_elide(key) if key is not None
                                                  else t("gui.actions.root"))))
    key_action.setObjectName("copy_key")
    key_action.triggered.connect(lambda _checked=False: copy_node_key(session, ref))
    if abs_path is None:
        _disable(key_action, t("gui.actions.no_path"))
    elif key is None:
        _disable(key_action, t("gui.actions.no_key"))

    menu.addSeparator()
    # -- filter / sort ---------------------------------------------------------------------
    in_record = getattr(ref, "record", None) is not None
    filter_action = menu.addAction(t("gui.actions.filter"))
    filter_action.setObjectName("filter_by_value")
    filter_action.triggered.connect(lambda _checked=False: _filter_by_value(session, ref, parent, owner))
    if not in_record:
        _disable(filter_action, t("gui.actions.filter.need_record"))
    elif node is not None and _is_container_kind(node) and getattr(node, "size", 0) > FILTER_MAX_BYTES:
        _disable(filter_action, t("gui.actions.filter.too_large_tip", size=format_size(node.size)))

    sort_menu = menu.addMenu(t("gui.actions.sort"))
    sort_menu.setObjectName("sort_menu")
    sort_menu.setToolTipsVisible(True)
    sortable = in_record and bool(getattr(ref, "path", ()))
    for name, desc in (("sort_asc", False), ("sort_desc", True)):
        action = sort_menu.addAction(t("gui.actions.sort.desc" if desc else "gui.actions.sort.asc"))
        action.setObjectName(name)
        action.triggered.connect(lambda _checked=False, d=desc: session.sort_by_field(ref.path, d))
        if not sortable:
            _disable(action, t("gui.actions.sort.need_field"))
    if not sortable:
        sort_menu.menuAction().setToolTip(t("gui.actions.sort.need_field"))
        sort_menu.setEnabled(False)

    # -- the view's own items --------------------------------------------------------------
    if extra:
        menu.addSeparator()
        _add_extra(menu, extra)

    menu.addSeparator()
    # -- navigation ------------------------------------------------------------------------
    full = menu.addAction(t("gui.actions.full_value"))
    full.setObjectName("full_value")
    full.triggered.connect(lambda _checked=False: show_full_value(parent, session, ref))

    reveal = menu.addAction(t("gui.actions.show_in_tree"))
    reveal.setObjectName("show_in_tree")
    reveal.triggered.connect(lambda _checked=False: session.select(ref, origin=view, reveal=True))
    if getattr(view, "view_id", None) == "tree":
        _disable(reveal, t("gui.actions.show_in_tree.already"))

    new_tab = menu.addAction(t("gui.actions.open_new_tab"))
    new_tab.setObjectName("open_new_tab")
    new_tab.triggered.connect(lambda _checked=False: _open_new_tab(session, ref))
    if not getattr(session, "path", None):
        _disable(new_tab, t("gui.actions.no_file"))
    return menu


def _show_shortcut(action: QAction, seq: QKeySequence) -> None:
    """Show ``seq`` next to a menu item without registering a competing shortcut (the
    main window owns the real one)."""
    action.setShortcut(seq)
    action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
    action.setShortcutVisibleInContextMenu(True)


def popup_node_menu(parent: QWidget, session: Any, ref: Any, global_pos: Any, *, view: Any = None,
                    extra: Iterable[ExtraItem] | None = None, node: Any = None) -> QMenu:
    """Build :func:`node_menu` and pop it up at ``global_pos`` (non-blocking); the menu
    deletes itself after it closed."""
    menu = node_menu(parent, session, ref, view=view, extra=extra, node=node)
    menu.aboutToHide.connect(menu.deleteLater)
    menu.popup(global_pos)
    return menu


def add_copy_actions(menu: QMenu, target: Callable[[], Any], *, shortcuts: bool = False) -> dict[str, QAction]:
    """Add the copy commands to ``menu`` (e.g. the main window's Edit menu); they act on
    ``target()`` -- the active :class:`~jsonreshape.gui.widgets.ViewBase` or ``None``.

    Returns ``{"value_<fmt>": action, "path_<style>": action, "key": action}``. With
    ``shortcuts`` the JSON value gets ``Ctrl+C`` and the dotted path ``Ctrl+Shift+C``
    (window shortcuts)."""
    actions: dict[str, QAction] = {}

    def run(method: str, *args: Any) -> None:
        view = target()
        if view is not None:
            getattr(view, method)(*args)

    for fmt in copyfmt.COPY_FORMATS:
        action = menu.addAction(t("gui.actions.copy_value", format=copyfmt.format_label(fmt)))
        action.setObjectName(f"copy_value_{fmt}")
        action.triggered.connect(lambda _checked=False, f=fmt: run("copy_value", f))
        actions[f"value_{fmt}"] = action
    menu.addSeparator()
    for style in copyfmt.PATH_STYLES:
        action = menu.addAction(t("gui.actions.copy_path_style", style=copyfmt.style_label(style)))
        action.setObjectName(f"copy_path_{style}")
        action.triggered.connect(lambda _checked=False, s=style: run("copy_path", s))
        actions[f"path_{style}"] = action
    key_action = menu.addAction(t("gui.actions.copy_key_plain"))
    key_action.setObjectName("copy_key")
    key_action.triggered.connect(lambda _checked=False: run("copy_key"))
    actions["key"] = key_action
    if shortcuts:
        actions["value_json"].setShortcut(QKeySequence(COPY_VALUE_SHORTCUT))
        actions["path_dotted"].setShortcut(QKeySequence(COPY_PATH_SHORTCUT))
    return actions
