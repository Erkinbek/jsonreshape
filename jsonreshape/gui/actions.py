"""The node context menu shared by the views (docs/ARCHITECTURE.md §11.7; AGENT_PROMPT §5).

:func:`node_menu` builds, for one :class:`~jsonreshape.gui.session.NodeRef`:

* **Nusxalash** (copy) submenu: the value as JSON (pretty, ``Ctrl+C`` shown) / compact JSON
  / Python / CSV row / CSV with header, the absolute path in the five styles (dotted with
  ``Ctrl+Shift+C`` shown; each item previews the path), the key;
* **Shu qiymat bo'yicha filtr** (record refs only; the value is loaded in a silent task,
  containers only up to :data:`FILTER_MAX_BYTES`) -> ``session.filter_by_value``;
* **Shu maydon bo'yicha tartiblash** ascending / descending (record refs with a non-empty
  path) -> ``session.sort_by_field``;
* the view's ``extra`` items (e.g. the tree's expand operations and its "Tanlangan ikkitasini
  taqqoslash");
* the stage-5 tools (:func:`add_tool_items`, docs/ARCHITECTURE.md §14.9): **Taqqoslash uchun
  belgilash** / **Belgilangan bilan taqqoslash** (:func:`jsonreshape.gui.comparewindow.node_menu_items`,
  the window's ``show_node`` reveals), **Statistika** (the window's ``show_stats`` with the node's field --
  array indices become ``[*]``; not for nodes outside the records), **Shu tugunni qismlarga bo'lish…**
  (:func:`jsonreshape.gui.filetoolsdialog.show_split_dialog` with the node as the source; refused for
  edited documents, JSON Lines / concatenated / brace-list files and scalar nodes);
* **To'liq ko'rish** (:class:`~jsonreshape.gui.widgets.FullValueDialog`), **Daraxtda
  ko'rsatish** (``session.select(ref, origin=view, reveal=True)``), **Yangi tabda ochish**
  (``open_requested`` with ``ref`` / ``record_path`` / ``format``).

Items that do not apply are disabled (never hidden) with a tooltip saying why. Every
action has an ``objectName`` (``copy_value_json``, ``copy_path_dotted``, ``copy_key``,
``filter_by_value``, ``sort_asc``, ``sort_desc``, ``compare_mark``, ``compare_with_marked``, ``stats``,
``split_node``, ``full_value``, ``show_in_tree``, ``open_new_tab``) so callers and tests can find it. The menu does no I/O while it is
built, except ``session.absolute_path(ref)`` (no data read for refs inside records).

:func:`add_copy_actions` gives the main window's Edit menu the same copy commands acting
on the active view.

Editing (docs/ARCHITECTURE.md §12.9)
====================================
:func:`node_menu` adds a **Tahrirlash** (edit) group after the copy menu when the view
implements the edit protocol below (or ``edit=True``): edit value, edit as JSON, rename,
insert ▸ (child / before / after), duplicate, delete, move up / down, change type ▸, paste.
The same commands (:data:`EDIT_COMMANDS`) fill the main window's Edit menu. Their keys
(Enter, Shift+Enter, F2, Ins, Del, Ctrl+D, Alt+↑/↓, Ctrl+V) are handled by the views'
grids, never as window or menu shortcuts (they would steal Enter/F2/Del from line edits in
the document area and from the menus themselves): menus show them **for display only**,
as the text after a tab character in the item's text (:func:`with_keys`; no ``QShortcut``
is registered). Object names: ``edit_<command name>``, the submenus ``edit_insert_menu`` /
``edit_type_menu`` (items ``edit_type_<kind>``).

View edit protocol (duck-typed like the other view calls; every method optional, they act
on the view's current node / selection): ``edit_value()``, ``edit_as_json()``,
``rename_key()``, ``insert_node(where)`` (``"child"`` | ``"before"`` | ``"after"``),
``duplicate_selection()``, ``delete_selection()``, ``move_selection(delta)`` (-1 up / +1
down), ``change_type(kind)`` (one of :data:`TYPE_KINDS`), ``paste_json()`` and
``edit_command_state(name) -> (enabled, reason) | None`` (a view may refine the state of
any command name of :data:`EDIT_COMMANDS` / ``"change_type"``). :data:`EditCommand.methods`
lists accepted alternative method names. Optional: ``edit_keys`` (a mapping command name ->
keys shown instead of :attr:`EditCommand.keys`, ``None`` = none; :func:`command_keys`) and
``change_type_kinds() -> kinds | None`` (the kinds of the nodes "change type" would convert):
the type submenu disables the conversions §12.4 always refuses for them
(:func:`change_type_refusal`, the rule of :func:`jsonreshape.gui.editors.type_menu`; without
the method the kind of the menu's ``node`` counts).

An item is disabled -- with the reason as its tooltip -- when the session has no edit
controller (``session.edit``), the controller is not available (its ``reason`` code, see
:func:`edit_reason_text`) or busy, the view lacks the method, the view says so, or the node
cannot take the command (a key rename of an array element, a child for a scalar, ...).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Callable, Iterable, NamedTuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QMenu, QWidget

from jsonreshape.errors import JsonReshapeError, UserInputError
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
from jsonreshape.i18n import has, t

log = logging.getLogger(__name__)

__all__ = ["EDIT_COMMANDS", "EditCommand", "FILTER_MAX_BYTES", "PATH_PREVIEW_CHARS", "TYPE_KINDS",
           "add_copy_actions", "add_edit_items", "add_tool_items", "change_type_refusal", "command_keys",
           "edit_command", "edit_command_state", "edit_reason_text",
           "node_menu", "popup_node_menu", "run_edit_command", "show_full_value", "split_node_refusal",
           "stats_refusal", "view_edit_method", "view_has_edit_commands", "with_keys"]

#: "Filter by this value" accepts an object/array only when its JSON is at most this big.
FILTER_MAX_BYTES = 4096
#: Paths shown in the copy-path items are elided to this many characters.
PATH_PREVIEW_CHARS = 60

COPY_VALUE_SHORTCUT = QKeySequence.StandardKey.Copy
COPY_PATH_SHORTCUT = "Ctrl+Shift+C"

ExtraItem = Any   # QAction | QMenu | None (separator) | (text, callback[, enabled[, tooltip]])


class EditCommand(NamedTuple):
    """One view edit command of the node menu / the main window's Edit menu: ``name``
    (object name ``edit_<name>``), the i18n key of its text, the key the views handle
    (display only), the view methods that carry it out (the first one the view has) and
    their arguments."""

    name: str
    text_key: str
    keys: str | None
    methods: tuple[str, ...]
    args: tuple = ()

    @property
    def method(self) -> str:
        return self.methods[0]


#: The view edit commands in menu order (docs/ARCHITECTURE.md §12.9). Their keys are handled
#: by the views' grids (never registered as shortcuts here).
EDIT_COMMANDS: tuple[EditCommand, ...] = (
    EditCommand("edit_value", "gui.actions.edit.value", "Enter", ("edit_value",)),
    EditCommand("edit_json", "gui.actions.edit.json", "Shift+Enter", ("edit_as_json",)),
    EditCommand("rename", "gui.actions.edit.rename", "F2", ("rename_key",)),
    EditCommand("insert_child", "gui.actions.edit.insert_child", None, ("insert_node",), ("child",)),
    EditCommand("insert_before", "gui.actions.edit.insert_before", None, ("insert_node",), ("before",)),
    EditCommand("insert_after", "gui.actions.edit.insert_after", None, ("insert_node",), ("after",)),
    EditCommand("duplicate", "gui.actions.edit.duplicate", "Ctrl+D", ("duplicate_selection",)),
    EditCommand("delete", "gui.actions.edit.delete", "Del", ("delete_selection",)),
    EditCommand("move_up", "gui.actions.edit.move_up", "Alt+Up", ("move_selection",), (-1,)),
    EditCommand("move_down", "gui.actions.edit.move_down", "Alt+Down", ("move_selection",), (1,)),
    EditCommand("paste", "gui.actions.edit.paste", "Ctrl+V", ("paste_json",)),
)
#: Node kinds offered by "change type" (``view.change_type(kind)``).
TYPE_KINDS: tuple[str, ...] = ("string", "number", "bool", "null", "object", "array")
#: The view method of "change type" (and alternatives).
CHANGE_TYPE_METHODS: tuple[str, ...] = ("change_type",)
#: Key shown next to the "insert" submenu (the views open their insert menu on it).
INSERT_KEYS = "Ins"
#: Controller ``reason`` codes with their own text (``gui.actions.edit.reason.<code>``).
EDIT_REASONS: tuple[str, ...] = ("indexing", "partial", "locked", "huge_single", "recovery_pending",
                                 "file_changed", "file_grown", "saving", "applying", "closed", "unavailable")
_COMMANDS = {c.name: c for c in EDIT_COMMANDS}


def edit_command(name: str) -> EditCommand:
    """The :class:`EditCommand` called ``name`` (``KeyError`` when unknown)."""
    return _COMMANDS[name]


def with_keys(text: str, keys: str | None) -> str:
    """``text`` with ``keys`` shown in a menu's shortcut column (``"text\\tKeys"``, native
    key names) without registering a shortcut."""
    if not keys:
        return text
    native = QKeySequence(keys).toString(QKeySequence.SequenceFormat.NativeText) or keys
    return f"{text}\t{native}"


def command_keys(view: Any, command: EditCommand | str) -> str | None:
    """The keys shown for ``command`` in ``view``'s menus: :attr:`EditCommand.keys`, unless the
    view's ``edit_keys`` mapping (command name -> keys, ``None`` = no key) says otherwise (the
    table moves records without keys: its Alt+Down opens the quick filter)."""
    cmd = command if isinstance(command, EditCommand) else _COMMANDS.get(str(command))
    if cmd is None:
        return None
    keys = getattr(view, "edit_keys", None) if view is not None else None
    if isinstance(keys, Mapping) and cmd.name in keys:
        value = keys[cmd.name]
        return str(value) if value else None
    return cmd.keys


def change_type_refusal(kinds: Iterable[str] | None, target: str) -> str | None:
    """Why "change type" to ``target`` is not offered for nodes of ``kinds`` (localized), or
    ``None`` (offered; unknown kinds too): the nodes have that type already, or §12.4 always refuses
    the conversion for one of them (:func:`jsonreshape.gui.editors.type_refusal`, the rule of the
    editors' type menu -- ``EditSession.change_type`` would refuse the whole command)."""
    kinds = tuple(dict.fromkeys(kinds or ()))
    if not kinds:
        return None
    if kinds == (target,):
        return t("gui.actions.edit.type.same")
    from jsonreshape.gui.editors import type_refusal

    return type_refusal(kinds, target)


def type_kinds(view: Any, node: Any = None) -> tuple[str, ...] | None:
    """The kinds of the nodes "change type" would convert in ``view``: its
    ``change_type_kinds()`` (the selection), else the kind of ``node``; ``None`` when unknown."""
    fn = getattr(view, "change_type_kinds", None) if view is not None else None
    if callable(fn):
        try:
            kinds = fn()
        except Exception:
            log.debug("change_type_kinds failed", exc_info=True)
            kinds = None
        if kinds:
            return tuple(kinds)
        return None
    kind = getattr(node, "kind", None) if node is not None else None
    return (str(kind),) if kind else None


def edit_reason_text(edit: Any) -> str:
    """Why ``edit`` (an ``EditController``) cannot edit now: its ``reason_text`` when it
    has one, else the localized text of its ``reason`` code."""
    if edit is None:
        return t("gui.actions.edit.no_editing")
    text = getattr(edit, "reason_text", None)
    if callable(text):
        try:
            text = text()
        except Exception:
            log.debug("reason_text failed", exc_info=True)
            text = None
    if isinstance(text, str) and text.strip():
        return text
    reason = str(getattr(edit, "reason", "") or "")
    key = f"gui.actions.edit.reason.{reason}"
    if reason and has(key):
        return t(key)
    return t("gui.actions.edit.reason.other", reason=reason or "?")


def view_edit_method(view: Any, command: EditCommand | str) -> Callable[..., Any] | None:
    """The bound method of ``view`` that carries ``command`` out (``None`` when the view has
    none of its method names)."""
    cmd = command if isinstance(command, EditCommand) else None
    names = cmd.methods if cmd is not None else (CHANGE_TYPE_METHODS if command == "change_type" else ())
    if view is None:
        return None
    for name in names:
        fn = getattr(view, name, None)
        if callable(fn):
            return fn
    return None


def _static_reason(session: Any, name: str, ref: Any, node: Any) -> str | None:
    """Reasons known without asking the view (the node's place / kind)."""
    if ref is None:
        return None
    path = tuple(getattr(ref, "path", ()) or ())
    record = getattr(ref, "record", None)
    doc = getattr(session, "doc", None)
    kind = getattr(node, "kind", None) if node is not None else None
    if name == "rename":
        if path and isinstance(path[-1], int) and not isinstance(path[-1], bool):
            return t("gui.actions.edit.no_key_element")
        if not path:
            if record is None:
                return t("gui.actions.edit.no_key_root")
            if doc is not None and not getattr(doc, "member_records", False):
                return t("gui.actions.edit.no_key_record")
    elif name == "insert_child":
        if kind is not None and kind not in ("object", "array"):
            return t("gui.actions.edit.need_container")
    elif name in ("insert_before", "insert_after", "move_up", "move_down", "duplicate"):
        if record is None and not path:
            return t("gui.actions.edit.root_no_siblings")
        if doc is not None and getattr(doc, "kind", None) == "single" and not path:
            return t("gui.actions.edit.root_no_siblings")
    return None


def edit_command_state(session: Any, view: Any, command: EditCommand | str, *, ref: Any = None,
                       node: Any = None) -> tuple[bool, str]:
    """``(enabled, reason)`` of an edit command for ``view`` of ``session`` (``command``: an
    :class:`EditCommand` or ``"change_type"``); ``ref`` / ``node``: the node it would act on
    when known (node menus), for the reasons that depend on it."""
    name = command.name if isinstance(command, EditCommand) else str(command)
    edit = getattr(session, "edit", None) if session is not None else None
    if edit is None:
        return False, t("gui.actions.edit.no_editing")
    if not bool(getattr(edit, "available", False)):
        return False, edit_reason_text(edit)
    if bool(getattr(edit, "busy", False)):
        return False, t("gui.actions.edit.busy")
    if view_edit_method(view, command) is None:
        return False, t("gui.actions.edit.not_here")
    fn = getattr(view, "edit_command_state", None)
    if callable(fn):
        try:
            state = fn(name)
        except Exception:
            log.debug("edit_command_state(%r) failed", name, exc_info=True)
            state = None
        if state is not None:
            ok, why = state
            if not ok:
                return False, str(why or t("gui.actions.edit.not_here"))
            return True, ""
    why = _static_reason(session, name, ref, node)
    if why:
        return False, why
    return True, ""


def run_edit_command(view: Any, command: EditCommand | str, *args: Any) -> bool:
    """Carry ``command`` out on ``view`` (``"change_type"`` takes the kind in ``args``);
    errors are reported through the session's application. False when the view has no
    method for it."""
    fn = view_edit_method(view, command)
    if fn is None:
        return False
    cmd_args = (command.args if isinstance(command, EditCommand) else ()) + tuple(args)
    try:
        fn(*cmd_args)
    except Exception as exc:
        log.exception("edit command %s failed", getattr(command, "name", command))
        session = getattr(view, "session", None)
        app = getattr(session, "app", None)
        report = getattr(app, "report_error", None)
        if callable(report):
            report(exc, view if isinstance(view, QWidget) else None)
    return True


def view_has_edit_commands(view: Any) -> bool:
    """The view implements at least one edit command."""
    return view is not None and (any(view_edit_method(view, c) is not None for c in EDIT_COMMANDS)
                                 or view_edit_method(view, "change_type") is not None)


def add_edit_items(menu: QMenu, session: Any, view: Any, *, ref: Any = None, node: Any = None) -> dict[str, Any]:
    """Add the edit group (see the module docstring) to ``menu``; returns
    ``{command name: QAction, "insert_menu": QMenu, "type_menu": QMenu}``."""
    items: dict[str, Any] = {}

    def add(cmd: EditCommand, target: QMenu) -> QAction:
        action = target.addAction(with_keys(t(cmd.text_key), command_keys(view, cmd)))
        action.setObjectName(f"edit_{cmd.name}")
        action.triggered.connect(lambda _checked=False, c=cmd: run_edit_command(view, c))
        ok, why = edit_command_state(session, view, cmd, ref=ref, node=node)
        if not ok:
            _disable(action, why)
        items[cmd.name] = action
        return action

    for name in ("edit_value", "edit_json", "rename"):
        add(_COMMANDS[name], menu)
    insert = menu.addMenu(t("gui.actions.edit.insert"))
    insert.setObjectName("edit_insert_menu")
    insert.setToolTipsVisible(True)
    insert.menuAction().setText(with_keys(t("gui.actions.edit.insert"), INSERT_KEYS))
    reasons = []
    for name in ("insert_child", "insert_before", "insert_after"):
        action = add(_COMMANDS[name], insert)
        if not action.isEnabled():
            reasons.append(action.toolTip())
    if len(reasons) == 3:
        insert.setEnabled(False)
        insert.menuAction().setToolTip(reasons[0])
    items["insert_menu"] = insert
    for name in ("duplicate", "delete", "move_up", "move_down"):
        add(_COMMANDS[name], menu)
    types = menu.addMenu(t("gui.actions.edit.change_type"))
    types.setObjectName("edit_type_menu")
    types.setToolTipsVisible(True)
    ok, why = edit_command_state(session, view, "change_type", ref=ref, node=node)
    kinds = type_kinds(view, node) if ok else None
    for kind in TYPE_KINDS:
        action = types.addAction(t(f"gui.actions.edit.type.{kind}"))
        action.setObjectName(f"edit_type_{kind}")
        action.setCheckable(True)
        action.setChecked(kinds == (kind,))
        action.triggered.connect(lambda _checked=False, k=kind: run_edit_command(view, "change_type", k))
        if not ok:
            _disable(action, why)
            continue
        refusal = change_type_refusal(kinds, kind)
        if refusal is not None:
            _disable(action, refusal)
    if not ok:
        types.setEnabled(False)
        types.menuAction().setToolTip(why)
    items["type_menu"] = types
    add(_COMMANDS["paste"], menu)
    return items


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


def _window_of(parent: Any) -> Any:
    """The top-level window of ``parent`` (the main window for a view inside it; ``None`` without one)."""
    try:
        return parent.window() if parent is not None else None
    except (AttributeError, RuntimeError):
        return None


def _location(session: Any, ref: Any) -> str:
    try:
        return str(session.location_text(ref))
    except Exception:
        log.debug("location_text(%r) failed", ref, exc_info=True)
        return str(getattr(ref, "path", ""))


def stats_refusal(session: Any, ref: Any, window: Any = None) -> str | None:
    """Why "Statistika" is not offered for ``ref`` (localized), or ``None``: the statistics describe a field of
    the records, so a node outside the records (the wrapper of a wrapped document) has none -- the root of a
    single document is its one record; the window must have a statistics dock (``show_stats``)."""
    doc = getattr(session, "doc", None)
    if doc is None or getattr(session, "closed", False):
        return t("gui.actions.no_document")
    if getattr(ref, "record", None) is None and getattr(doc, "kind", None) != "single":
        return t("gui.actions.stats.need_record")
    if not callable(getattr(window, "show_stats", None)):
        return t("gui.actions.tools.unavailable")
    return None


def split_node_refusal(session: Any, ref: Any, node: Any = None) -> str | None:
    """Why "Shu tugunni qismlarga bo'lish…" is not offered (localized), or ``None``: the refusals of
    :func:`jsonreshape.gui.filetoolsdialog.check_node_source` (edited documents, JSON Lines / concatenated /
    brace-list files) and a node known to be a scalar (``node.kind``; an unknown kind is left to the split
    dialog's plan, which refuses a scalar with the same text)."""
    doc = getattr(session, "doc", None)
    if doc is None or getattr(session, "closed", False):
        return t("gui.actions.no_document")
    from jsonreshape.gui.filetoolsdialog import check_node_source

    try:
        check_node_source(doc)
    except JsonReshapeError as exc:
        return exc.message
    kind = getattr(node, "kind", None) if node is not None else None
    if kind is not None and kind not in ("object", "array"):
        return t("gui.filetools.error.node_scalar", path=_location(session, ref))
    return None


def _show_stats(window: Any, path: tuple) -> None:
    from jsonreshape.gui.statspanel import field_for_path

    window.show_stats(field_for_path(path))


def _split_node(window: Any, session: Any, ref: Any) -> Any:
    from jsonreshape.gui.filetoolsdialog import show_split_dialog

    try:
        return show_split_dialog(window if isinstance(window, QWidget) else None, session, source=ref)
    except Exception as exc:
        log.exception("opening the split dialog failed")
        session.app.report_error(exc, window if isinstance(window, QWidget) else None)
        return None


def add_tool_items(menu: QMenu, parent: Any, session: Any, ref: Any, *, node: Any = None) -> dict[str, QAction]:
    """The stage-5 group of :func:`node_menu` (docs/ARCHITECTURE.md §14.9): "Taqqoslash uchun belgilash" /
    "Belgilangan bilan taqqoslash" (:func:`jsonreshape.gui.comparewindow.node_menu_items`; the reveal callback
    is the window's ``show_node``), "Statistika" (the window's ``show_stats`` with the node's field, array
    indices as ``[*]``) and "Shu tugunni qismlarga bo'lish…" (:func:`~jsonreshape.gui.filetoolsdialog
    .show_split_dialog` with the node as the source). Returns ``{object name: action}`` (``compare_mark``,
    ``compare_with_marked``, ``stats``, ``split_node``); items that do not apply are disabled with the reason."""
    window = _window_of(parent)
    items: dict[str, QAction] = {}
    menu.addSeparator()
    try:
        from jsonreshape.gui import comparewindow

        compare = comparewindow.node_menu_items(window, session, ref, reveal=getattr(window, "show_node", None))
    except Exception:
        log.exception("the compare items of the node menu failed")
        compare = []
    before = list(menu.actions())
    _add_extra(menu, compare)
    added = [a for a in menu.actions() if a not in before]
    for action, name in zip(added, ("compare_mark", "compare_with_marked")):
        action.setObjectName(name)
        items[name] = action
    menu.addSeparator()
    stats = menu.addAction(t("gui.statspanel.action.node"))
    stats.setObjectName("stats")
    why = stats_refusal(session, ref, window)
    if why:
        _disable(stats, why)
    else:
        stats.setToolTip(t("gui.statspanel.action.node.tip"))
        stats.triggered.connect(lambda _checked=False, w=window, p=tuple(getattr(ref, "path", ()) or ()):
                                _show_stats(w, p))
    items["stats"] = stats
    split = menu.addAction(t("gui.filetools.action.split_node"))
    split.setObjectName("split_node")
    why = split_node_refusal(session, ref, node)
    if why:
        _disable(split, why)
    else:
        split.setToolTip(t("gui.filetools.action.split_node.tip"))
        split.triggered.connect(lambda _checked=False, w=window: _split_node(w, session, ref))
    items["split_node"] = split
    return items


def node_menu(parent: QWidget, session: Any, ref: Any, *, view: Any = None,
              extra: Iterable[ExtraItem] | None = None, node: Any = None, edit: bool | None = None) -> QMenu:
    """The context menu of ``ref`` (see the module doc). ``view``: the view asking (the
    ``origin`` of "show in tree"; a tree disables that item); ``extra``: items inserted
    after the filter/sort section; ``node``: optional already-known node info (any object
    with ``kind`` and ``size``) used to disable "filter" for big containers at once (and
    the edit commands that need a container); ``edit``: the edit group (``None``: when the
    view implements the edit protocol). The caller owns the returned menu (see
    :func:`popup_node_menu`)."""
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

    # -- edit (§12.9) ----------------------------------------------------------------------
    if edit is None:
        edit = view_has_edit_commands(view)
    if edit:
        menu.addSeparator()
        add_edit_items(menu, session, view, ref=ref, node=node)

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

    # -- stage 5: compare, statistics, split -----------------------------------------------
    add_tool_items(menu, parent, session, ref, node=node)

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
                    extra: Iterable[ExtraItem] | None = None, node: Any = None, edit: bool | None = None) -> QMenu:
    """Build :func:`node_menu` and pop it up at ``global_pos`` (non-blocking); the menu
    deletes itself after it closed."""
    menu = node_menu(parent, session, ref, view=view, extra=extra, node=node, edit=edit)
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
