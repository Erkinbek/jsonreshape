"""The command palette (Ctrl+Shift+P; docs/ARCHITECTURE.md §15.0, AGENT_PROMPT §11 "Buyruqlar palitrasi").

:func:`show_palette` opens one :class:`CommandPalette` per main window: a non-modal popup over the window (a child
frame at the top of the document area, never a separate window) with a line edit and a list. The entries are
collected each time it opens (:func:`collect_entries`), so new actions appear without any registration:

* every ``QAction`` with a text reached from the menu bar (:func:`walk_menu_bar`; submenus become the path
  ``"Fayl ▸ So'nggi fayllar"``; dynamic menus are filled first through their ``aboutToShow``) -- File, Edit (the
  current view's edit commands with their display keys), View, Navigate, Search, Tools (stage 5), Help -- and the
  window's other actions (``MainWindow.acts``) that no menu shows; placeholders (a disabled item without an object
  name or keys, e.g. "Ro'yxat bo'sh") are no commands and are left out;
* the current node's context-menu commands of the active view (path ``"Tugun ▸ …"``), built the way the view
  builds its menu (:func:`view_menu`: the tree's ``context_menu(row)``, the table's ``cell_menu(row, col)``, the
  text view's ``build_context_menu()``, else :func:`jsonreshape.gui.actions.node_menu` for the view's
  ``current_ref()``); the commands the Edit menu already has (the edit group, the copy items) are left out;
* ``"Tab: <name>"`` for every open tab and ``"Ochish: <file>"`` for every recent file (``GuiState``; nothing is read
  from the disk: a missing file is handled by ``MainWindow.open_recent``).

Each row shows the text (``&`` mnemonics removed), the menu path and the current keys (``QAction.shortcuts()``, so
the user's keys from :mod:`jsonreshape.gui.keymap`; the Edit menu's display-only keys after the tab). Disabled
commands are listed greyed with their reason (the action's tooltip when it says more than its text, else a generic
reason) and cannot run.

Matching (:func:`rank`): the query and the texts are folded like the full-file search
(:func:`jsonreshape.textmatch.fold` with ``translit``: case folding, every apostrophe variant equal, Uzbek Cyrillic
read as Latin -- "сақлаш" finds "Saqlash", "o'zgar" / "oʻzgar" find "o'zgarish"); the query is folded in both
contexts of the Cyrillic ``е`` (``ye`` at a word start, ``e`` after a consonant). Ranking: the whole query as an
exact prefix of the text, then at a word start (:func:`jsonreshape.textmatch.is_word_char` decides word
boundaries: an apostrophe inside a word is part of it), then the initials of words, then anywhere in a word, then
as a subsequence; a query of several words is also matched word by word (each word in the text or, ranked lower,
in the menu path). Ties: enabled before disabled, the earlier match, the recently used, the shorter text, the menu
order. An empty query lists the last :data:`RECENT_MAX` commands run from the palette first (``GuiState`` key
:data:`STATE_RECENT`, most recent first), then everything in menu order.

Keys: typing filters at once (a few milliseconds for hundreds of entries; texts are folded once and cached);
↑ ↓ PgUp PgDn Home End move; Enter (or a click) runs; Esc closes. A command runs after the popup closed and the
focus went back to the widget that had it (else the active view): ``QTimer.singleShot(0, …)``. Clicking or
focusing anything outside the popup closes it. Colours come from the application palette and
:func:`jsonreshape.gui.theme.color` roles (theme-aware, redrawn on ``theme_changed``).
"""

from __future__ import annotations

import functools
import logging
import os
import time
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Sequence

from PySide6.QtCore import QAbstractListModel, QEvent, QModelIndex, QObject, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QFont, QFontMetrics, QKeySequence, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QLineEdit,
    QListView,
    QMenu,
    QMenuBar,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from jsonreshape import textmatch
from jsonreshape.gui import theme
from jsonreshape.i18n import t

log = logging.getLogger(__name__)

__all__ = ["CommandPalette", "PATH_SEP", "PaletteEntry", "RECENT_MAX", "Ranked", "STATE_RECENT", "clean_text",
           "collect_entries", "display_keys", "fold_text", "is_keyboard_key", "palette_of", "rank", "recent_ids",
           "remember", "show_palette", "view_menu", "walk_menu_bar"]

#: GUI state key of the recently run palette commands (entry ids, most recent first).
STATE_RECENT = "palette_recent"
#: How many recently run commands are remembered (and listed first for an empty query).
RECENT_MAX = 20
#: Between the levels of a menu path ("Fayl ▸ Saqlash").
PATH_SEP = " \N{BLACK RIGHT-POINTING SMALL TRIANGLE} "
#: Rows shown without scrolling.
VISIBLE_ROWS = 10
NATIVE = QKeySequence.SequenceFormat.NativeText

# ranking tiers (lower is better)
TIER_PREFIX, TIER_WORD, TIER_INITIALS, TIER_INSIDE, TIER_FUZZY = 0, 1, 2, 3, 4
TIER_PATH, TIER_PATH_INSIDE = 5, 6
#: Added to a query of several words matched word by word (a match of the whole query ranks first).
TERMS_BASE = 10


# ==================================================================================
# text helpers
# ==================================================================================
def clean_text(text: str) -> tuple[str, str]:
    """``(label, display keys)`` of a menu text: the ``&`` mnemonics removed (``&&`` is a literal ``&``) and the
    part after a tab (keys shown for display only, :func:`jsonreshape.gui.actions.with_keys`) split off."""
    label, _sep, keys = str(text or "").partition("\t")
    label = label.replace("&&", "\0").replace("&", "").replace("\0", "&")
    return label.strip(), keys.strip()


def _plain(text: str) -> str:
    """A text compared with a tooltip: mnemonics, the display keys and ellipses removed (Qt's default tooltip
    is the text stripped this way)."""
    label = clean_text(text)[0]
    return label.replace("\N{HORIZONTAL ELLIPSIS}", "").replace("...", "").strip()


_SHOWN_PLAIN_KEYS = frozenset({
    Qt.Key.Key_Escape, Qt.Key.Key_Tab, Qt.Key.Key_Backtab, Qt.Key.Key_Backspace, Qt.Key.Key_Return, Qt.Key.Key_Enter,
    Qt.Key.Key_Insert, Qt.Key.Key_Delete, Qt.Key.Key_Pause, Qt.Key.Key_Print, Qt.Key.Key_Home, Qt.Key.Key_End,
    Qt.Key.Key_Left, Qt.Key.Key_Up, Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown,
    Qt.Key.Key_Menu})


def is_keyboard_key(seq: QKeySequence) -> bool:
    """``seq`` is typed on an ordinary keyboard: with a modifier, a function key, a character or an editing /
    navigation key -- not a hardware "Open" / "Copy" / "Zoom In" key that ``QKeySequence.StandardKey`` adds (they
    stay assigned; they are only not shown)."""
    if seq.isEmpty():
        return False
    combo = seq[0]
    mods = combo.keyboardModifiers() & ~Qt.KeyboardModifier.KeypadModifier
    key = combo.key()
    if mods != Qt.KeyboardModifier.NoModifier:
        return True
    value = key.value if hasattr(key, "value") else int(key)
    if value < 0x01000000:
        return True                                   # a character key
    if Qt.Key.Key_F1.value <= value <= Qt.Key.Key_F35.value:
        return True
    return key in _SHOWN_PLAIN_KEYS


def display_keys(action: QAction, limit: int = 3) -> str:
    """The keys of ``action`` as the user reads them (native text, ``", "`` between several, at most ``limit``;
    hardware-only keys are left out: :func:`is_keyboard_key`); the display-only keys after a tab in its text when
    it has no shortcut."""
    seqs = [s for s in action.shortcuts() if is_keyboard_key(s)]
    if seqs:
        return ", ".join(s.toString(NATIVE) for s in seqs[:limit])
    return clean_text(action.text())[1]


@dataclass(frozen=True, slots=True)
class _Folded:
    text: str                       # folded (case, apostrophes, Cyrillic -> Latin)
    imap: tuple[int, ...]           # folded index -> index in the original text (+ sentinel)
    starts: frozenset[int]          # folded indices where a word starts
    start_list: tuple[int, ...]     # the same, ascending


@functools.lru_cache(maxsize=16384)
def _fold(text: str) -> _Folded:
    folded, imap = textmatch.fold(text, translit=True)
    starts = tuple(i for i in range(len(folded))
                   if textmatch.is_word_char(folded, i) and (i == 0 or not textmatch.is_word_char(folded, i - 1)))
    return _Folded(folded, tuple(imap), frozenset(starts), starts)


def fold_text(text: str) -> str:
    """``text`` folded for matching (:func:`jsonreshape.textmatch.fold` with ``translit``; cached)."""
    return _fold(str(text)).text


@functools.lru_cache(maxsize=512)
def _forms(term: str) -> tuple[str, ...]:
    """The foldings of a query word: as typed (a Cyrillic ``е`` at its start reads ``ye``) and after a consonant
    (``e``) -- the word may continue a word of the text."""
    first = textmatch.fold(term, translit=True)[0]
    after = textmatch.fold("\N{CYRILLIC SMALL LETTER BE}" + term, translit=True)[0][1:]
    return tuple(dict.fromkeys(f for f in (first, after) if f))


# ==================================================================================
# entries
# ==================================================================================
@dataclass(eq=False)
class PaletteEntry:
    """One command of the palette. ``id`` is stable across openings (the recent list): ``act:<MainWindow.acts
    key>``, ``menu:<path>/<text>`` (a menu item without a key), ``node:<object name or text>``,
    ``tab:<normalized path>``, ``open:<normalized path>``. ``kind``: ``action`` | ``node`` | ``tab`` | ``open``.
    ``run`` carries the command out (only called while ``enabled``; an ``action`` entry re-checks its
    ``QAction``)."""

    id: str
    text: str
    path: str = ""
    keys: str = ""
    enabled: bool = True
    reason: str = ""
    kind: str = "action"
    run: Callable[[], Any] | None = None
    checked: bool | None = None
    tooltip: str = ""
    action: QAction | None = None
    order: int = 0

    def can_run(self) -> bool:
        """Enabled now (an action entry asks its ``QAction`` again: the state may have changed meanwhile)."""
        if not self.enabled or self.run is None:
            return False
        if self.action is not None:
            try:
                return bool(self.action.isEnabled())
            except RuntimeError:          # the action was deleted (its menu was rebuilt)
                return False
        return True


def walk_menu_bar(menubar: QMenuBar, *, refresh: bool = False) -> Iterator[tuple[QAction, tuple[str, ...], bool]]:
    """``(action, menu path, every menu on the path enabled)`` for each visible, non-separator action with a text
    under ``menubar``, depth first in menu order (an action in two menus comes once). With ``refresh`` each menu's
    ``aboutToShow`` is emitted before it is read (menus filled when they open -- the recent files -- are filled and
    the enabled states brought up to date)."""
    seen: set[int] = set()
    for top in list(menubar.actions()):
        if top.isSeparator() or not top.isVisible():
            continue
        menu = top.menu()
        if isinstance(menu, QMenu):
            yield from _walk_menu(menu, (clean_text(top.text())[0],), top.isEnabled(), refresh, seen)
        elif clean_text(top.text())[0] and id(top) not in seen:
            seen.add(id(top))
            yield top, (), True


def _walk_menu(menu: QMenu, path: tuple[str, ...], enabled: bool, refresh: bool,
               seen: set[int]) -> Iterator[tuple[QAction, tuple[str, ...], bool]]:
    if refresh:
        try:
            menu.aboutToShow.emit()
        except Exception:  # a filler failing must not break the palette
            log.exception("filling the menu %r failed", menu.objectName())
    on = enabled and menu.isEnabled()
    for action in list(menu.actions()):
        if action.isSeparator() or not action.isVisible():
            continue
        sub = action.menu()
        if isinstance(sub, QMenu):
            yield from _walk_menu(sub, path + (clean_text(action.text())[0],), on and action.isEnabled(), refresh,
                                  seen)
            continue
        if not clean_text(action.text())[0] or id(action) in seen:
            continue
        seen.add(id(action))
        yield action, path, on


def _action_reason(action: QAction, menu_enabled: bool, menu_reason: str, generic: str) -> str:
    """Why ``action`` cannot run: its tooltip when that says more than its text (the edit commands put their
    reason there), the reason of a disabled menu on its path, else ``generic``."""
    tip = (action.toolTip() or "").strip()
    base = str(action.property("base_tip") or "").strip()
    if tip and _plain(tip) != _plain(action.text()) and tip != base:
        return tip
    if not menu_enabled and menu_reason:
        return menu_reason
    return generic


def _entry_for_action(action: QAction, ident: str, path: tuple[str, ...], menu_on: bool, generic: str,
                      kind: str = "action", menu_reason: str = "") -> PaletteEntry:
    label, _keys = clean_text(action.text())
    enabled = bool(action.isEnabled()) and menu_on
    path_text = PATH_SEP.join(p for p in path if p)
    reason = "" if enabled else _action_reason(action, menu_on, menu_reason, generic)
    tip = action.toolTip() if action.toolTip() and _plain(action.toolTip()) != _plain(label) else ""
    ref = weakref.ref(action)

    def run() -> None:
        target = ref()
        if target is not None:
            target.trigger()

    return PaletteEntry(id=ident, text=label, path=path_text, keys=display_keys(action), enabled=enabled,
                        reason=reason, kind=kind, run=run, checked=action.isChecked() if action.isCheckable()
                        else None, tooltip=tip if enabled else "", action=action)


def view_menu(view: Any) -> QMenu | None:
    """The context menu of the current node of ``view``, built the way the view builds it for a right click (not
    shown; the caller owns it): an optional ``palette_menu()``, the tree's ``context_menu(row)`` for its current
    row, the table's ``cell_menu(row, col)`` for its current cell, a ``build_context_menu()`` (the text view),
    else :func:`jsonreshape.gui.actions.node_menu` for the view's ``current_ref()``. ``None``: no node."""
    if view is None:
        return None
    try:
        fn = getattr(view, "palette_menu", None)
        if callable(fn):
            menu = fn()
            return menu if isinstance(menu, QMenu) else None
        grid = getattr(view, "grid", None)
        current = grid.current() if grid is not None and callable(getattr(grid, "current", None)) else None
        context = getattr(view, "context_menu", None)
        if callable(context) and current is not None:
            return context(current[0]) if current[0] >= 0 else None
        cell = getattr(view, "cell_menu", None)
        if callable(cell) and current is not None:
            return cell(current[0], current[1]) if current[0] >= 0 and current[1] >= 0 else None
        build = getattr(view, "build_context_menu", None)
        if callable(build):
            return build()
        session = getattr(view, "session", None)
        ref_fn = getattr(view, "current_ref", None)
        ref = ref_fn() if callable(ref_fn) else None
        if session is None or ref is None or getattr(session, "doc", None) is None:
            return None
        from jsonreshape.gui.actions import node_menu

        return node_menu(view, session, ref, view=view)
    except Exception:
        log.exception("building the node menu for the palette failed")
        return None


def _menu_items(menu: QMenu, path: tuple[str, ...]) -> Iterator[tuple[QAction, tuple[str, ...], bool, str]]:
    """``(action, path, enabled along the path, reason of a disabled submenu)`` of a context menu."""

    def walk(m: QMenu, p: tuple[str, ...], on: bool, why: str) -> Iterator[tuple[QAction, tuple[str, ...], bool,
                                                                               str]]:
        for action in list(m.actions()):
            if action.isSeparator() or not action.isVisible():
                continue
            sub = action.menu()
            if isinstance(sub, QMenu):
                sub_on = on and sub.isEnabled() and action.isEnabled()
                sub_why = why or ("" if sub_on else (action.toolTip() or ""))
                yield from walk(sub, p + (clean_text(action.text())[0],), sub_on, sub_why)
            elif clean_text(action.text())[0]:
                yield action, p, on, why

    yield from walk(menu, path, menu.isEnabled(), "")


def document_area(window: Any) -> QWidget | None:
    """The main window's document area: its ``central`` widget (tabs / welcome page) -- not
    ``centralWidget()``, which is a holder (bar + document area) while the "restore the last session?" bar of
    :mod:`jsonreshape.gui.workspace` is shown -- else ``centralWidget()``."""
    central = getattr(window, "central", None)
    if isinstance(central, QWidget):
        return central
    fn = getattr(window, "centralWidget", None)
    widget = fn() if callable(fn) else None
    return widget if isinstance(widget, QWidget) else None


def _norm_path(path: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(path))
    except (TypeError, ValueError):
        return str(path)


def collect_entries(window: Any, *, refresh: bool = True, owned: list[Any] | None = None) -> list[PaletteEntry]:
    """Every command of the palette for ``window`` now (see the module docstring), in menu order. Menus built for
    the node commands are appended to ``owned`` (the caller deletes them after the command ran)."""
    entries: list[PaletteEntry] = []
    has_tab = callable(getattr(window, "current_tab", None)) and window.current_tab() is not None
    generic = t("gui.palette.reason.unavailable") if has_tab else t("gui.palette.reason.no_file")
    update = getattr(window, "_update_actions", None)
    if refresh and callable(update):
        try:
            update()
        except Exception:
            log.exception("updating the actions for the palette failed")
    acts: dict[str, QAction] = dict(getattr(window, "acts", {}) or {})
    key_of = {id(action): key for key, action in acts.items()}
    names: set[str] = set()
    menubar = window.menuBar() if callable(getattr(window, "menuBar", None)) else None
    seen: set[int] = set()
    if isinstance(menubar, QMenuBar):
        for action, path, on in walk_menu_bar(menubar, refresh=refresh):
            if _is_recent_file(window, action) or _is_placeholder(action):
                continue                       # the recent files are the "Ochish: …" entries
            key = key_of.get(id(action))
            ident = f"act:{key}" if key else "menu:" + "/".join(path + (clean_text(action.text())[0],))
            entries.append(_entry_for_action(action, ident, path, on, generic))
            seen.add(id(action))
            if action.objectName():
                names.add(action.objectName())
    # window actions no menu shows (window / document-area level)
    central = document_area(window)
    for key, action in acts.items():
        if id(action) in seen or not clean_text(action.text())[0]:
            continue
        holders = action.associatedObjects()
        if not any(h is window or (central is not None and h is central) for h in holders):
            continue
        entries.append(_entry_for_action(action, f"act:{key}", (), True, generic))
    # the current node's context-menu commands
    view = window.active_view() if callable(getattr(window, "active_view", None)) else None
    menu = view_menu(view) if refresh else None
    if menu is not None:
        if owned is not None:
            owned.append(menu)
        root = (t("gui.palette.node"),)
        for action, path, on, why in _menu_items(menu, root):
            name = action.objectName()
            if name.startswith("edit_") or (name and name in names) or _is_placeholder(action):
                continue                       # the Edit menu has the same command (same view, same node)
            ident = "node:" + (name or "/".join(path[1:] + (clean_text(action.text())[0],)))
            entries.append(_entry_for_action(action, ident, path, on, generic, kind="node", menu_reason=why))
    # open tabs (the commands keep no strong reference to the window or a tab)
    wref = weakref.ref(window)
    tabs = window.tab_widgets() if callable(getattr(window, "tab_widgets", None)) else []
    current = window.current_tab() if has_tab else None
    seen_tabs: dict[str, int] = {}
    for tab in tabs:
        session = getattr(tab, "session", None)
        path = str(getattr(session, "path", "") or "")
        name_fn = getattr(tab, "display_name", None)
        name = name_fn() if callable(name_fn) else os.path.basename(path)
        key = _norm_path(path)
        n = seen_tabs.get(key, 0)
        seen_tabs[key] = n + 1
        entries.append(PaletteEntry(
            id=f"tab:{key}" + (f"#{n + 1}" if n else ""), text=t("gui.palette.tab", name=name),
            path=os.path.normpath(path) if path else "", kind="tab", checked=True if tab is current else None,
            run=lambda r=weakref.ref(tab): _focus_tab(wref(), r())))
    # recent files
    state = getattr(window, "state", None)
    recent = state.recent_files() if state is not None and callable(getattr(state, "recent_files", None)) else []
    can_open = callable(getattr(window, "open_recent", None))
    for path in recent:
        entries.append(PaletteEntry(
            id=f"open:{_norm_path(path)}", text=t("gui.palette.open", name=os.path.basename(path) or path),
            path=os.path.normpath(path), kind="open", run=(lambda p=path: _open_recent(wref(), p)) if can_open
            else None, enabled=can_open))
    for i, entry in enumerate(entries):
        entry.order = i
    return entries


def _is_placeholder(action: QAction) -> bool:
    """A disabled menu item without an object name or keys: information such as "Ro'yxat bo'sh" (an empty recent
    list), not a command (every command of the window and the node menus has an object name)."""
    return not action.isEnabled() and not action.objectName() and not action.shortcuts()


def _is_recent_file(window: Any, action: QAction) -> bool:
    """An item of the recent files menu other than "clear the list" (the files are the "Ochish: …" entries; the
    "no recent files" placeholder is no command)."""
    menus = getattr(window, "menus", None) or {}
    recent = menus.get("recent") if isinstance(menus, dict) else None
    if recent is None:
        return False
    return action in recent.actions() and action.objectName() != "recent_clear"


def _focus_tab(window: Any, tab: Any) -> None:
    if window is None or tab is None or getattr(tab, "closed", False):
        return
    tabs = getattr(window, "tabs", None)
    if tabs is not None and tabs.indexOf(tab) >= 0:
        tabs.setCurrentWidget(tab)


def _open_recent(window: Any, path: str) -> None:
    """"Ochish: …": ``MainWindow.open_recent`` (an open file's tab is focused; a missing file leaves the list)."""
    if window is not None:
        window.open_recent(path)


# ==================================================================================
# ranking (pure)
# ==================================================================================
@dataclass(slots=True)
class Ranked:
    """An entry in the filtered list: its sort ``key`` and the matched character indices of its text."""

    entry: PaletteEntry
    key: tuple = ()
    hits: tuple[int, ...] = ()
    recent: bool = False


@dataclass(slots=True)
class _Hit:
    tier: int
    pos: int
    idx: Sequence[int] = field(default_factory=tuple)


def _initials(form: str, folded: _Folded) -> list[int] | None:
    """``form`` spelled by the first letters of consecutive-or-later words (``"bs"`` -> "Boshqacha Saqlash")."""
    if len(form) < 2 or " " in form:
        return None
    out: list[int] = []
    k = 0
    for i in folded.start_list:
        if k < len(form) and folded.text[i] == form[k]:
            out.append(i)
            k += 1
    return out if k == len(form) else None


def _subsequence(form: str, text: str) -> list[int] | None:
    if len(form) > len(text):
        return None
    out: list[int] = []
    pos = 0
    for ch in form:
        if ch == " ":
            continue
        pos = text.find(ch, pos)
        if pos < 0:
            return None
        out.append(pos)
        pos += 1
    return out or None


def _match(form: str, folded: _Folded, *, fuzzy: bool = True) -> _Hit | None:
    """The best way ``form`` (a folded query) occurs in ``folded``."""
    text = folded.text
    pos = text.find(form)
    if pos == 0:
        return _Hit(TIER_PREFIX, 0, range(len(form)))
    first_inside = None
    while pos > 0:
        if pos in folded.starts:
            return _Hit(TIER_WORD, pos, range(pos, pos + len(form)))
        if first_inside is None:
            first_inside = pos
        pos = text.find(form, pos + 1)
    if fuzzy:
        initials = _initials(form, folded)
        if initials:
            return _Hit(TIER_INITIALS, initials[0], initials)
    if first_inside is not None:
        return _Hit(TIER_INSIDE, first_inside, range(first_inside, first_inside + len(form)))
    if fuzzy:
        seq = _subsequence(form, text)
        if seq:
            return _Hit(TIER_FUZZY, seq[0], seq)
    return None


def _best(forms: Iterable[str], folded: _Folded, *, fuzzy: bool = True) -> _Hit | None:
    best: _Hit | None = None
    for form in forms:
        hit = _match(form, folded, fuzzy=fuzzy)
        if hit is not None and (best is None or (hit.tier, hit.pos) < (best.tier, best.pos)):
            best = hit
    return best


def _score(entry: PaletteEntry, whole: tuple[str, ...], terms: list[tuple[str, ...]]) -> tuple[int, int,
                                                                                                 set[int]] | None:
    """``(total tier, first position, matched folded indices)`` of ``entry`` or ``None`` (no match)."""
    text = _fold(entry.text)
    if len(terms) == 1:
        hit = _best(terms[0], text)
        if hit is not None:
            return hit.tier, hit.pos, set(hit.idx)
        path_hit = _best(terms[0], _fold(entry.path), fuzzy=False) if entry.path else None
        if path_hit is not None:
            return (TIER_PATH if path_hit.tier <= TIER_WORD else TIER_PATH_INSIDE), 1 << 20, set()
        return None
    hit = _best(whole, text, fuzzy=False)
    if hit is not None:
        return hit.tier, hit.pos, set(hit.idx)
    total = TERMS_BASE
    first = 1 << 20
    idx: set[int] = set()
    path = _fold(entry.path) if entry.path else None
    for forms in terms:
        term_hit = _best(forms, text)
        if term_hit is not None:
            total += term_hit.tier
            first = min(first, term_hit.pos)
            idx.update(term_hit.idx)
            continue
        path_hit = _best(forms, path, fuzzy=False) if path is not None else None
        if path_hit is None:
            return None
        total += TIER_PATH if path_hit.tier <= TIER_WORD else TIER_PATH_INSIDE
    return total, first, idx


def rank(entries: Sequence[PaletteEntry], query: str, recent: Sequence[str] = ()) -> list[Ranked]:
    """The entries matching ``query`` in display order (see the module docstring); an empty query: the entries
    whose ids are in ``recent`` (that order, at most :data:`RECENT_MAX`), then the others in menu order."""
    recent_rank: dict[str, int] = {}
    for ident in recent:
        if len(recent_rank) >= RECENT_MAX:
            break
        recent_rank.setdefault(ident, len(recent_rank))
    text = str(query or "").strip()
    if text.startswith(">"):
        text = text[1:].strip()
    if not text:
        firsts: list[tuple[int, PaletteEntry]] = []
        rest: list[PaletteEntry] = []
        taken: set[str] = set()
        for entry in entries:
            r = recent_rank.get(entry.id)
            if r is not None and entry.id not in taken:
                taken.add(entry.id)
                firsts.append((r, entry))
            else:
                rest.append(entry)
        firsts.sort(key=lambda item: item[0])
        return [Ranked(e, (0, r), (), True) for r, e in firsts] + [Ranked(e, (1, e.order)) for e in rest]
    words = text.split()
    whole = _forms(" ".join(words))
    terms = [_forms(w) for w in words]
    terms = [f for f in terms if f]
    if not terms or not whole:
        return []
    out: list[Ranked] = []
    for entry in entries:
        scored = _score(entry, whole, terms)
        if scored is None:
            continue
        total, first, idx = scored
        r = recent_rank.get(entry.id, RECENT_MAX)
        key = (total, not entry.enabled, first, r, len(entry.text), entry.order)
        imap = _fold(entry.text).imap
        hits = tuple(sorted({imap[i] for i in idx if 0 <= i < len(imap) - 1}))
        out.append(Ranked(entry, key, hits, r < RECENT_MAX))
    out.sort(key=lambda item: item.key)
    return out


def remember(state: Any, ident: str) -> list[str]:
    """Put ``ident`` first in the recent palette commands of ``state`` (a ``GuiState``; at most
    :data:`RECENT_MAX`); returns the new list (a broken stored value is replaced)."""
    old = state.get(STATE_RECENT) if state is not None else None
    ids = [x for x in old if isinstance(x, str)] if isinstance(old, list) else []
    ids = [ident] + [x for x in ids if x != ident]
    del ids[RECENT_MAX:]
    if state is not None:
        try:
            state.set(STATE_RECENT, ids)
        except (TypeError, ValueError):
            log.debug("could not store the recent palette commands", exc_info=True)
    return ids


def recent_ids(state: Any) -> list[str]:
    """The recent palette command ids stored in ``state`` (anything malformed is ignored)."""
    value = state.get(STATE_RECENT) if state is not None else None
    if not isinstance(value, list):
        return []
    return [x for x in value if isinstance(x, str)][:RECENT_MAX]


# ==================================================================================
# the popup
# ==================================================================================
_ROLE_ENTRY = Qt.ItemDataRole.UserRole + 1


class _Model(QAbstractListModel):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.rows: list[Ranked] = []

    def set_rows(self, rows: list[Ranked]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802, B008 (Qt API)
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        ranked = self.rows[index.row()]
        entry = ranked.entry
        if role == Qt.ItemDataRole.DisplayRole:
            return entry.text
        if role == _ROLE_ENTRY:
            return ranked
        if role == Qt.ItemDataRole.ToolTipRole:
            parts = [entry.path] if entry.path else []
            if not entry.enabled:
                parts.append(t("gui.palette.disabled_tip", reason=entry.reason))
            elif entry.tooltip:
                parts.append(entry.tooltip)
            return "\n".join(parts) or None
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{entry.text} {entry.keys}".strip()
        return None


class _Delegate(QStyledItemDelegate):
    """Two lines per entry: the text (matched characters bold, a check mark for a checked action) with the keys at
    the right; under it the menu path -- or, for a disabled entry, its reason (italic). Disabled entries are
    painted with the palette's disabled text colour."""

    def __init__(self, palette_widget: CommandPalette) -> None:
        super().__init__(palette_widget)
        self._owner = palette_widget

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802 (Qt API)
        fm = QFontMetrics(option.font)
        small = QFontMetrics(self._small_font(option.font))
        return QSize(200, fm.height() + small.height() + 10)

    @staticmethod
    def _small_font(font: QFont) -> QFont:
        small = QFont(font)
        size = font.pointSizeF()
        if size > 0:
            small.setPointSizeF(max(6.0, size * 0.88))
        return small

    def paint(self, painter: Any, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        ranked = index.data(_ROLE_ENTRY)
        if not isinstance(ranked, Ranked):
            return super().paint(painter, option, index)
        entry = ranked.entry
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        pal = opt.palette
        if selected:                  # readable on the selection; a disabled entry shows its reason (italic)
            fg = pal.color(QPalette.ColorGroup.Normal, QPalette.ColorRole.HighlightedText)
            muted = fg
        elif entry.enabled:
            fg = pal.color(QPalette.ColorGroup.Normal, QPalette.ColorRole.Text)
            muted = theme.color("muted")
        else:
            fg = muted = pal.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        rect = opt.rect.adjusted(8, 4, -8, -4)
        font = QFont(opt.font)
        fm = QFontMetrics(font)
        small = self._small_font(font)
        sfm = QFontMetrics(small)
        painter.save()
        try:
            # keys at the right of the first line
            keys = entry.keys
            keys_w = fm.horizontalAdvance(keys) + 2 if keys else 0
            if keys:
                painter.setFont(font)
                painter.setPen(muted)
                painter.drawText(QRect(rect.right() - keys_w + 1, rect.top(), keys_w, fm.height()),
                                 int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), keys)
                keys_w += 12                     # a gap before the text
            x = rect.left()
            if entry.checked:
                mark = "\N{CHECK MARK} "
                painter.setFont(font)
                painter.setPen(fg)
                painter.drawText(QRect(x, rect.top(), fm.horizontalAdvance(mark), fm.height()),
                                 int(Qt.AlignmentFlag.AlignVCenter), mark)
                x += fm.horizontalAdvance(mark)
            self._draw_runs(painter, entry.text, set(ranked.hits), QRect(x, rect.top(), rect.right() - keys_w - x,
                                                                        fm.height()), font, fg)
            # second line: the path, or why the command cannot run
            second = entry.reason if not entry.enabled else entry.path
            if ranked.recent and entry.enabled:
                tag = t("gui.palette.recent_tag")
                tag_w = sfm.horizontalAdvance(tag) + 8
                painter.setFont(small)
                painter.setPen(muted)
                painter.drawText(QRect(rect.right() - tag_w, rect.top() + fm.height() + 2, tag_w, sfm.height()),
                                 int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), tag)
            else:
                tag_w = 0
            if second:
                small.setItalic(not entry.enabled)
                painter.setFont(small)
                painter.setPen(muted)
                width = rect.width() - tag_w
                text = QFontMetrics(small).elidedText(second, Qt.TextElideMode.ElideMiddle, max(0, width))
                painter.drawText(QRect(rect.left(), rect.top() + fm.height() + 2, width, sfm.height()),
                                 int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
        finally:
            painter.restore()

    @staticmethod
    def _draw_runs(painter: Any, text: str, hits: set[int], rect: QRect, font: QFont, color: Any) -> None:
        """``text`` with the characters at ``hits`` bold; elided at the right when it does not fit."""
        bold = QFont(font)
        bold.setBold(True)
        fm, bfm = QFontMetrics(font), QFontMetrics(bold)
        painter.setPen(color)
        if not hits:
            painter.setFont(font)
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             fm.elidedText(text, Qt.TextElideMode.ElideRight, max(0, rect.width())))
            return
        runs: list[tuple[str, bool]] = []
        for i, ch in enumerate(text):
            on = i in hits
            if runs and runs[-1][1] == on:
                runs[-1] = (runs[-1][0] + ch, on)
            else:
                runs.append((ch, on))
        total = sum((bfm if on else fm).horizontalAdvance(s) for s, on in runs)
        if total > rect.width():                 # does not fit: plain elided text
            painter.setFont(font)
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             fm.elidedText(text, Qt.TextElideMode.ElideRight, max(0, rect.width())))
            return
        x = rect.left()
        for s, on in runs:
            f = bold if on else font
            w = (bfm if on else fm).horizontalAdvance(s)
            painter.setFont(f)
            painter.drawText(QRect(x, rect.top(), w + 1, rect.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), s)
            x += w


class _QueryEdit(QLineEdit):
    """The query line: the list keys never become window shortcuts while it has the focus."""

    LIST_KEYS = {Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown, Qt.Key.Key_Home,
                 Qt.Key.Key_End, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Escape}

    def event(self, event: QEvent) -> bool:  # noqa: D401 - Qt API
        if event.type() == QEvent.Type.ShortcutOverride and event.key() in self.LIST_KEYS:
            event.accept()
            return True
        return super().event(event)


class CommandPalette(QFrame):
    """The popup (see the module docstring). ``ran`` is emitted with the entry after its command was started."""

    ran = Signal(object)
    closed = Signal()

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self.setObjectName("command_palette")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAutoFillBackground(True)
        self._window = weakref.ref(window)
        self._entries: list[PaletteEntry] = []
        self._owned: list[Any] = []              # node menus built for this opening
        self._return_focus: Any = None
        self._is_open = False
        self.open_ms = 0.0                       # duration of the last open() (tests / benchmarks)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        self.edit = _QueryEdit(self)
        self.edit.setObjectName("palette_query")
        self.edit.setPlaceholderText(t("gui.palette.placeholder"))
        self.edit.setClearButtonEnabled(True)
        self.edit.textChanged.connect(self._filter)
        self.edit.installEventFilter(self)
        layout.addWidget(self.edit)
        self.model = _Model(self)
        self.list = QListView(self)
        self.list.setObjectName("palette_list")
        self.list.setModel(self.model)
        self.list.setItemDelegate(_Delegate(self))
        self.list.setUniformItemSizes(True)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setSelectionMode(QListView.SelectionMode.SingleSelection)
        self.list.setMouseTracking(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.clicked.connect(self._clicked)
        layout.addWidget(self.list)
        self.empty_label = QLabel(t("gui.palette.empty"), self)
        self.empty_label.setObjectName("palette_empty")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setVisible(False)
        layout.addWidget(self.empty_label)
        self.status = QLabel(self)
        self.status.setObjectName("palette_status")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.hide()
        self._restyle()
        theme.notifier().theme_changed.connect(self._restyle)
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._on_focus_changed)
        window.installEventFilter(self)

    # ------------------------------------------------------------------ public
    def is_open(self) -> bool:
        """The popup is shown (between :meth:`open` and :meth:`close_palette`)."""
        return self._is_open

    def open(self) -> None:
        """Collect the entries now and show the popup with the focus in its (empty) query line."""
        start = time.perf_counter()
        window = self._window()
        if window is None:
            return
        if not self._is_open:
            focus = QApplication.focusWidget()
            self._return_focus = weakref.ref(focus) if focus is not None and not self._inside(focus) else None
        self._drop_owned()
        self._entries = collect_entries(window, refresh=True, owned=self._owned)
        self.edit.blockSignals(True)
        self.edit.clear()
        self.edit.blockSignals(False)
        self._filter()
        self._is_open = True
        self._place()
        self.show()
        self.raise_()
        self.edit.setFocus(Qt.FocusReason.PopupFocusReason)
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
            app.installEventFilter(self)
        self.open_ms = (time.perf_counter() - start) * 1000.0

    def query(self) -> str:
        return self.edit.text()

    def set_query(self, text: str) -> None:
        """Type ``text`` into the query line (filters at once)."""
        self.edit.setText(text)

    def all_entries(self) -> list[PaletteEntry]:
        """Every entry collected at the last :meth:`open`."""
        return list(self._entries)

    def entries(self) -> list[PaletteEntry]:
        """The entries shown now, in display order."""
        return [r.entry for r in self.model.rows]

    def ranked(self) -> list[Ranked]:
        return list(self.model.rows)

    def current_row(self) -> int:
        index = self.list.currentIndex()
        return index.row() if index.isValid() else -1

    def current_entry(self) -> PaletteEntry | None:
        row = self.current_row()
        return self.model.rows[row].entry if 0 <= row < len(self.model.rows) else None

    def set_current_row(self, row: int) -> None:
        count = len(self.model.rows)
        if count == 0:
            return
        row = max(0, min(count - 1, row))
        index = self.model.index(row, 0)
        self.list.setCurrentIndex(index)
        self.list.scrollTo(index)
        self._show_current_status()

    def run_current(self) -> bool:
        """Enter: run the current entry (see :meth:`run_entry`)."""
        entry = self.current_entry()
        if entry is None:
            return False
        return self.run_entry(entry)

    def run_entry(self, entry: PaletteEntry) -> bool:
        """Run ``entry``: refused with its reason while it cannot run (the popup stays); else it is remembered as
        recent, the popup closes, the focus goes back and the command runs right after (event loop)."""
        if not entry.can_run():
            reason = entry.reason or t("gui.palette.reason.unavailable")
            self.status.setText(t("gui.palette.cannot_run", reason=reason))
            self._style_status(True)
            return False
        window = self._window()
        state = getattr(window, "state", None)
        remember(state, entry.id)
        save = getattr(getattr(window, "app", None), "save_state", None)
        if callable(save):
            try:
                save()
            except Exception:
                log.debug("saving the GUI state failed", exc_info=True)
        owned, self._owned = self._owned, []          # the node menu lives until its command ran
        self.close_palette()
        context = window if isinstance(window, QObject) else self
        QTimer.singleShot(0, context, lambda e=entry, o=owned: self._run_later(e, o))
        return True

    def close_palette(self, *, restore_focus: bool = True) -> None:
        """Esc: hide the popup; the focus goes back to the widget that had it (else the active view). The entries
        and the node menu built for this opening are released (they hold the node's session)."""
        if not self._is_open:
            return
        self._is_open = False
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self.hide()
        self._drop_owned()
        self._entries = []
        self.model.set_rows([])
        if restore_focus:
            self._restore_focus()
        self.closed.emit()

    # ------------------------------------------------------------------ internals
    def _run_later(self, entry: PaletteEntry, owned: list[Any]) -> None:
        try:
            if entry.can_run() and entry.run is not None:
                entry.run()
                self.ran.emit(entry)
        except Exception as exc:
            log.exception("the palette command %s failed", entry.id)
            window = self._window()
            report = getattr(getattr(window, "app", None), "report_error", None)
            if callable(report):
                report(exc, window)
        finally:
            for obj in owned:
                try:
                    obj.deleteLater()
                except RuntimeError:  # pragma: no cover - deleted meanwhile
                    pass

    def _drop_owned(self) -> None:
        owned, self._owned = self._owned, []
        for obj in owned:
            try:
                obj.deleteLater()
            except RuntimeError:  # pragma: no cover
                pass

    def _restore_focus(self) -> None:
        target = self._return_focus() if self._return_focus is not None else None
        self._return_focus = None
        try:
            if target is not None and target.isVisible() and target.isEnabled():
                target.setFocus(Qt.FocusReason.PopupFocusReason)
                return
        except RuntimeError:              # deleted meanwhile
            pass
        window = self._window()
        view = window.active_view() if window is not None and callable(getattr(window, "active_view", None)) \
            else None
        if isinstance(view, QWidget) and view.isVisible():
            view.setFocus(Qt.FocusReason.PopupFocusReason)

    def _inside(self, widget: Any) -> bool:
        return widget is self or (isinstance(widget, QWidget) and self.isAncestorOf(widget))

    def _filter(self, *_args: Any) -> None:
        window = self._window()
        recent = recent_ids(getattr(window, "state", None))
        rows = rank(self._entries, self.edit.text(), recent)
        self.model.set_rows(rows)
        self.list.setVisible(bool(rows))
        self.empty_label.setVisible(not rows)
        if rows:
            self.set_current_row(0)
        else:
            self._show_current_status()
        self._fit()

    def _show_current_status(self) -> None:
        entry = self.current_entry()
        shown, total = len(self.model.rows), len(self._entries)
        if entry is not None and not entry.enabled:
            self.status.setText(t("gui.palette.disabled_tip", reason=entry.reason))
            self._style_status(True)
            return
        self.status.setText(t("gui.palette.status", shown=shown, total=total))
        self._style_status(False)

    def _style_status(self, warn: bool) -> None:
        role = "warn" if warn else "muted"
        self.status.setStyleSheet(f"color: {theme.color(role).name()};")

    def _move(self, key: int) -> None:
        count = len(self.model.rows)
        if not count:
            return
        row = self.current_row()
        page = max(1, VISIBLE_ROWS - 1)
        if key == Qt.Key.Key_Up:
            row = count - 1 if row <= 0 else row - 1
        elif key == Qt.Key.Key_Down:
            row = 0 if row >= count - 1 else row + 1
        elif key == Qt.Key.Key_PageUp:
            row = max(0, row - page)
        elif key == Qt.Key.Key_PageDown:
            row = min(count - 1, row + page)
        elif key == Qt.Key.Key_Home:
            row = 0
        elif key == Qt.Key.Key_End:
            row = count - 1
        self.set_current_row(row)

    def _clicked(self, index: QModelIndex) -> None:
        if index.isValid():
            self.set_current_row(index.row())
            entry = self.current_entry()
            if entry is not None:
                self.run_entry(entry)

    def _fit(self) -> None:
        rows = len(self.model.rows)
        height = self.list.sizeHintForRow(0) if rows else 0
        self.list.setFixedHeight(max(1, min(rows, VISIBLE_ROWS)) * max(height, 1) + 2 * self.list.frameWidth()
                                 if rows else 0)
        if self._is_open:
            self._place()

    def _place(self) -> None:
        window = self._window()
        if window is None:
            return
        central = document_area(window)
        if central is not None and central.isVisible() and window.isAncestorOf(central):
            area = QRect(central.mapTo(window, QPoint(0, 0)), central.size())    # below a bar above it
        else:
            area = window.rect()
        width = min(max(420, int(window.width() * 0.55)), 720, max(200, window.width() - 24))
        self.setFixedWidth(width)
        self.adjustSize()
        x = area.x() + max(0, (area.width() - width) // 2)
        y = area.y() + 8
        self.move(x, y)

    def _restyle(self) -> None:
        panel = theme.color("panel").name()
        line = theme.color("line").name()
        self.setStyleSheet(f"QFrame#command_palette {{ background: {panel}; border: 1px solid {line}; "
                           f"border-radius: 6px; }}")
        self._style_status(False)
        self.list.viewport().update()

    def _on_focus_changed(self, _old: Any, now: Any) -> None:
        if self._is_open and isinstance(now, QWidget) and not self._inside(now):
            self.close_palette(restore_focus=False)     # the user went elsewhere

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt API)
        etype = event.type()
        if obj is self.edit and etype == QEvent.Type.KeyPress:
            key = event.key()
            shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            if key in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown) or \
                    (key in (Qt.Key.Key_Home, Qt.Key.Key_End) and not shift):
                self._move(key)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.run_current()
                return True
            if key == Qt.Key.Key_Escape:
                self.close_palette()
                return True
            return False
        if etype == QEvent.Type.MouseButtonPress and self._is_open and isinstance(obj, QWidget) \
                and obj.isWidgetType() and not self._inside(obj):
            self.close_palette(restore_focus=False)       # a click elsewhere closes it (the click goes on)
            return False
        if obj is self._window() and etype == QEvent.Type.Resize and self._is_open:
            self._place()
        return False


def palette_of(window: QWidget) -> CommandPalette | None:
    """The palette of ``window`` when it was opened before."""
    found = window.findChild(CommandPalette, "command_palette")
    return found if isinstance(found, CommandPalette) else None


def show_palette(window: QWidget) -> CommandPalette:
    """Ctrl+Shift+P: open the palette of ``window`` (one per window, created on first use); when it is open
    already, its entries are collected again and the query is cleared."""
    palette = palette_of(window)
    if palette is None:
        palette = CommandPalette(window)
    palette.open()
    return palette
