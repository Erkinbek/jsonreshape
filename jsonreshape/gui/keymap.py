"""Keyboard settings: the user's keys over the main window's default shortcuts (docs/ARCHITECTURE.md §15.0 "Key
settings"; AGENT_PROMPT §11 "Tugmalar sozlamalarda o'zgartiriladi").

:class:`KeyMap` (one per main window, ``MainWindow.keymap``, made by :meth:`KeyMap.attach` right after the actions
and menus exist):

* the ids are the keys of ``MainWindow.acts`` (the ``_act`` keys, plus the Edit menu's "copy as" items and the dock
  toggles); the view edit commands of the Edit menu (``edit_*``: Enter, F2, Del, ... shown for display only) are not
  configurable -- their keys belong to the grids (§12.9) and are listed as fixed keys;
* the defaults are the shortcuts the actions have when :meth:`KeyMap.attach` runs (what ``_create_actions`` set;
  an action made later is captured the first time the map looks at it);
* the user's overrides live in the GUI state, ``gui_state["shortcuts"] = {id: ["Ctrl+Alt+E", ...]}`` (Qt portable
  key texts; an empty list = no key; the CLI has no use for them), and are applied at start and live
  (:meth:`KeyMap.set_keys`, :meth:`KeyMap.reset`, :meth:`KeyMap.set_overrides`; other windows follow
  ``settings_changed``). The menus and the command palette show the keys the actions have, i.e. the current ones.

Rules (the same everywhere):

* one key combination per sequence (``Ctrl+K, Ctrl+S`` chords are refused: a chord's first key would be taken from
  every widget of the window while Qt waits for the second);
* a key used by another action is refused with that action's name and menu path (two actions on one key cancel
  each other in Qt);
* the grids' own keys (:data:`FIXED_KEYS`: arrows, PgUp/PgDn/Home/End and their Shift / Ctrl forms, Ctrl+A,
  Ctrl+Space, ``*``, ``+``, ``-``, Space, Enter, Shift+Enter, F2, Ins, Del, Ctrl+D, Alt+↑/↓ -- Alt+↓ is the table's
  quick filter --, Ctrl+V, Shift+Ins, Ctrl+C, Ctrl+Shift+C, Esc, Tab / Shift+Tab) and the menu bar's Alt+letter keys
  are refused for every action except one that has that key by default (Ctrl+C copies the value, ``*`` expands
  everything, Ctrl+Shift+C copies the path: the window-level forms of the same commands). Esc, Tab and the
  navigation keys are added to the specified list for safety: a window shortcut on them would take them from the
  inline editors, line edits and grids.

A stored value that is not a mapping is ignored (logged; it is replaced only when the user changes a key); entries
of unknown ids are kept as they are (an action of another version); invalid key texts, refused keys and keys
another override already took are dropped when the keys are applied (logged). An override takes its key from an
action that has it by default (the user's choice wins; that action keeps its other keys).

:class:`KeymapPage` is the "Klaviatura" page of :class:`~jsonreshape.gui.settingsdialog.SettingsDialog`: a filter
box, the table (command, menu path, default keys, current keys, an "Asl holiga" button per row), a
``QKeySequenceEdit`` recording the selected command's new key (one combination; "Tugmasiz" removes its keys),
"Hammasini asl holiga", a message line (refusals name the other command) and the read-only list of the fixed keys.
It edits a working copy that the dialog's "Saqlash" applies (:meth:`KeymapPage.apply`); "Bekor qilish" drops it. The
dialog's "Standartga qaytarish" leaves the keys alone (the page has its own reset).
"""

from __future__ import annotations

import logging
import weakref
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from PySide6.QtCore import QKeyCombination, QObject, Qt, Signal
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from jsonreshape.gui import theme
from jsonreshape.gui.palette import PATH_SEP, clean_text, document_area, fold_text, is_keyboard_key, walk_menu_bar
from jsonreshape.i18n import t

log = logging.getLogger(__name__)

__all__ = ["FIXED_KEYS", "FixedKey", "KeyMap", "KeyRow", "KeymapPage", "STATE_KEY", "keymap_of", "native_text",
           "normalize_key"]

#: GUI state key of the user's overrides.
STATE_KEY = "shortcuts"
#: Stored sequences per action at most (longer lists are cut).
MAX_KEYS = 8
PORTABLE = QKeySequence.SequenceFormat.PortableText
NATIVE = QKeySequence.SequenceFormat.NativeText


@dataclass(frozen=True)
class FixedKey:
    """Keys the views handle themselves (never window shortcuts): ``keys`` (normalized portable texts), ``shown``
    (their display), ``what`` (the i18n key of what they do)."""

    keys: tuple[str, ...]
    shown: str
    what: str


_ARROWS = "\N{UPWARDS ARROW} \N{DOWNWARDS ARROW} \N{LEFTWARDS ARROW} \N{RIGHTWARDS ARROW}"
#: The grids' own keys (docs/ARCHITECTURE.md §11.5, §12.9): listed read-only, refused for window actions.
FIXED_KEYS: tuple[FixedKey, ...] = (
    FixedKey(("Up", "Down", "Left", "Right"), _ARROWS, "gui.keymap.fixed.move"),
    FixedKey(("PgUp", "PgDown", "Home", "End", "Ctrl+Home", "Ctrl+End"), "PgUp, PgDn, Home, End, Ctrl+Home, Ctrl+End",
             "gui.keymap.fixed.page"),
    FixedKey(("Shift+Up", "Shift+Down", "Shift+Left", "Shift+Right", "Shift+PgUp", "Shift+PgDown", "Shift+Home",
              "Shift+End", "Ctrl+Shift+Home", "Ctrl+Shift+End"), "Shift + " + _ARROWS + ", PgUp, PgDn, Home, End",
             "gui.keymap.fixed.extend"),
    FixedKey(("Ctrl+A",), "Ctrl+A", "gui.keymap.fixed.select_all"),
    FixedKey(("Ctrl+Space",), "Ctrl+Space", "gui.keymap.fixed.toggle_select"),
    FixedKey(("*",), "*", "gui.keymap.fixed.expand_all"),
    FixedKey(("+", "-"), "+  -", "gui.keymap.fixed.expand_collapse"),
    FixedKey(("Space",), "Space", "gui.keymap.fixed.full_value"),
    FixedKey(("Return", "Enter"), "Enter", "gui.keymap.fixed.edit"),
    FixedKey(("Shift+Return", "Shift+Enter"), "Shift+Enter", "gui.keymap.fixed.edit_json"),
    FixedKey(("F2",), "F2", "gui.keymap.fixed.rename"),
    FixedKey(("Ins",), "Ins", "gui.keymap.fixed.insert"),
    FixedKey(("Del",), "Del", "gui.keymap.fixed.delete"),
    FixedKey(("Ctrl+D",), "Ctrl+D", "gui.keymap.fixed.duplicate"),
    FixedKey(("Alt+Up", "Alt+Down"), "Alt+\N{UPWARDS ARROW}  Alt+\N{DOWNWARDS ARROW}", "gui.keymap.fixed.move_node"),
    FixedKey(("Alt+Down",), "Alt+\N{DOWNWARDS ARROW}", "gui.keymap.fixed.quick_filter"),
    FixedKey(("Ctrl+V", "Shift+Ins"), "Ctrl+V, Shift+Ins", "gui.keymap.fixed.paste"),
    FixedKey(("Ctrl+C",), "Ctrl+C", "gui.keymap.fixed.copy_value"),
    FixedKey(("Ctrl+Shift+C",), "Ctrl+Shift+C", "gui.keymap.fixed.copy_path"),
    FixedKey(("Esc",), "Esc", "gui.keymap.fixed.cancel"),
    FixedKey(("Tab", "Shift+Tab", "Backtab", "Shift+Backtab"), "Tab, Shift+Tab", "gui.keymap.fixed.focus"),
)


def _sequence(key: Any) -> QKeySequence:
    if isinstance(key, QKeySequence):
        return key
    return QKeySequence(str(key), PORTABLE)


def normalize_key(key: Any) -> str | None:
    """The portable text of one key combination (``"Ctrl+Alt+E"``; the keypad flag dropped: ``Num+*`` is ``*``),
    ``None`` when ``key`` is not exactly one valid combination (empty, unknown key, a modifier alone, a chord, not
    a text or a ``QKeySequence``)."""
    if not isinstance(key, (str, QKeySequence)):
        return None
    try:
        seq = _sequence(key)
    except (TypeError, ValueError):
        return None
    if seq.count() != 1:
        return None
    try:
        combo = seq[0]
        k = combo.key()
        if k in _NOT_KEYS:
            return None
        mods = combo.keyboardModifiers() & ~Qt.KeyboardModifier.KeypadModifier
        text = QKeySequence(QKeyCombination(mods, k)).toString(PORTABLE)
    except (TypeError, ValueError, IndexError):
        return None
    return text or None


_NOT_KEYS = frozenset({Qt.Key.Key_unknown, Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Meta,
                       Qt.Key.Key_AltGr})


def native_text(keys: Iterable[str], *, keyboard_only: bool = False) -> str:
    """Portable key texts as the user reads them (native, ``", "`` between several). ``keyboard_only``: the
    hardware "Open" / "Copy" / "Zoom In" keys of the standard shortcuts are left out
    (:func:`jsonreshape.gui.palette.is_keyboard_key`) unless nothing else is left."""
    keys = list(keys)
    if keyboard_only:
        typed = [k for k in keys if is_keyboard_key(_sequence(k))]
        keys = typed or keys
    out = []
    for key in keys:
        text = _sequence(key).toString(NATIVE)
        out.append(text or str(key))
    return ", ".join(out)


_FIXED: dict[str, FixedKey] = {}
for _fk in FIXED_KEYS:
    for _k in _fk.keys:
        _norm = normalize_key(_k)
        if _norm is not None:
            _FIXED.setdefault(_norm, _fk)


def fixed_key(key: Any) -> FixedKey | None:
    """The fixed (grid) key ``key`` is, if any."""
    norm = normalize_key(key)
    return _FIXED.get(norm) if norm is not None else None


@dataclass
class KeyRow:
    """One configurable action: ``id``, its text, menu path, default and current keys (portable texts)."""

    id: str
    text: str
    path: str
    default: list[str] = field(default_factory=list)
    current: list[str] = field(default_factory=list)


def _clean_list(value: Any) -> list[str] | None:
    """A stored value as a list of key texts (``None``: unusable)."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return None
    return [v for v in value if isinstance(v, str)][:MAX_KEYS]


class KeyMap(QObject):
    """See the module docstring. ``changed`` is emitted after the keys were applied anew."""

    changed = Signal()

    def __init__(self, window: Any, *, state: Any = None, save: Any = None) -> None:
        super().__init__(window if isinstance(window, QObject) else None)
        self._window = weakref.ref(window)
        self._state = state if state is not None else getattr(window, "state", None)
        app = getattr(window, "app", None)
        self._save = save if save is not None else getattr(app, "save_state", None)
        self._defaults: dict[str, list[str]] = {}
        self._overrides: dict[str, list[str]] = {}
        self._unknown: dict[str, list[str]] = {}
        signal = getattr(app, "settings_changed", None)
        if signal is not None:
            signal.connect(self.reload)

    @classmethod
    def attach(cls, window: Any) -> KeyMap:
        """The map of ``window``: its actions' current keys become the defaults, the stored overrides are applied."""
        keymap = cls(window)
        keymap.capture()
        keymap.reload()
        return keymap

    # ------------------------------------------------------------------ actions
    def window(self) -> Any:
        return self._window()

    def actions(self) -> dict[str, QAction]:
        """The configurable actions by id (see the module docstring)."""
        window = self._window()
        if window is None:
            return {}
        acts = getattr(window, "acts", None) or {}
        central = document_area(window)            # window.central (a session bar may wrap it)
        out: dict[str, QAction] = {}
        for key, action in list(acts.items()):
            try:
                if not clean_text(action.text())[0]:
                    continue
                if key.startswith("edit_"):
                    holders = action.associatedObjects()
                    if not any(h is window or (central is not None and h is central) for h in holders):
                        continue          # a view edit command: display-only keys, handled by the grids
            except RuntimeError:          # deleted
                continue
            out[key] = action
        return out

    def capture(self) -> None:
        """Remember the defaults of actions not seen before (their keys now)."""
        for key, action in self.actions().items():
            if key not in self._defaults:
                self._defaults[key] = [s.toString(PORTABLE) for s in action.shortcuts() if not s.isEmpty()]

    def ids(self) -> list[str]:
        """The configurable ids in menu order (actions no menu shows last)."""
        self.capture()
        actions = self.actions()
        order: list[str] = []
        window = self._window()
        menubar = window.menuBar() if window is not None and callable(getattr(window, "menuBar", None)) else None
        key_of = {id(a): k for k, a in actions.items()}
        if menubar is not None:
            for action, _path, _on in walk_menu_bar(menubar):
                key = key_of.get(id(action))
                if key is not None and key not in order:
                    order.append(key)
        order.extend(k for k in actions if k not in order)
        return order

    def defaults(self, ident: str) -> list[str]:
        self.capture()
        return list(self._defaults.get(ident, []))

    def label(self, ident: str) -> str:
        action = self.actions().get(ident)
        return clean_text(action.text())[0] if action is not None else ident

    def paths(self) -> dict[str, str]:
        """The menu path of each configurable id (``""``: in no menu)."""
        window = self._window()
        menubar = window.menuBar() if window is not None and callable(getattr(window, "menuBar", None)) else None
        out: dict[str, str] = {}
        if menubar is None:
            return out
        key_of = {id(a): k for k, a in self.actions().items()}
        for action, path, _on in walk_menu_bar(menubar):
            key = key_of.get(id(action))
            if key is not None and key not in out:
                out[key] = PATH_SEP.join(path)
        return out

    def rows(self, overrides: Mapping[str, list[str]] | None = None) -> list[KeyRow]:
        """Every configurable action with its keys (under ``overrides``, default: the applied ones)."""
        final, _dropped = self.resolve(self._overrides if overrides is None else overrides)
        paths = self.paths()
        return [KeyRow(ident, self.label(ident), paths.get(ident, ""), self.defaults(ident), list(final.get(ident, [])))
                for ident in self.ids()]

    def current(self, ident: str) -> list[str]:
        """The keys ``ident`` has now (portable texts)."""
        action = self.actions().get(ident)
        return [s.toString(PORTABLE) for s in action.shortcuts() if not s.isEmpty()] if action is not None else []

    def overrides(self) -> dict[str, list[str]]:
        """The user's overrides of known actions (copies)."""
        return {k: list(v) for k, v in self._overrides.items()}

    def _mnemonics(self) -> dict[str, str]:
        """``Alt+<letter>`` of the menu bar's menus -> the menu's name."""
        window = self._window()
        menubar = window.menuBar() if window is not None and callable(getattr(window, "menuBar", None)) else None
        out: dict[str, str] = {}
        if menubar is None:
            return out
        for action in menubar.actions():
            text = action.text() or ""
            i = 0
            while True:
                i = text.find("&", i)
                if i < 0 or i + 1 >= len(text):
                    break
                if text[i + 1] == "&":
                    i += 2
                    continue
                norm = normalize_key("Alt+" + text[i + 1].upper())
                if norm is not None:
                    out.setdefault(norm, clean_text(text)[0])
                break
        return out

    # ------------------------------------------------------------------ resolution
    def resolve(self, overrides: Mapping[str, list[str]]) -> tuple[dict[str, list[str]], list[tuple[str, str, str]]]:
        """``(keys per id, dropped)`` under ``overrides``: each override keeps its valid keys that are not fixed /
        menu keys (unless the action's own defaults) and not taken by an earlier override (menu order); every other
        action -- and one whose stored keys were all unusable (an empty list is "no key" and stays) -- keeps its
        defaults minus the keys an override took. ``dropped``: ``(id, key, why)``."""
        self.capture()
        ids = self.ids()
        fixed = _FIXED
        mnemonics = self._mnemonics()
        taken: dict[str, str] = {}
        final: dict[str, list[str]] = {}
        dropped: list[tuple[str, str, str]] = []
        fallback: set[str] = set()
        for ident in ids:
            if ident not in overrides:
                continue
            own = {normalize_key(k) for k in self._defaults.get(ident, [])}
            keep: list[str] = []
            wanted = list(overrides[ident] or [])
            for key in wanted:
                norm = normalize_key(key)
                if norm is None:
                    dropped.append((ident, str(key), "invalid"))
                    continue
                if norm not in own and (norm in fixed or norm in mnemonics):
                    dropped.append((ident, norm, "fixed"))
                    continue
                if norm in taken and taken[norm] != ident:
                    dropped.append((ident, norm, f"taken by {taken[norm]}"))
                    continue
                if norm not in taken:
                    keep.append(norm)
                taken[norm] = ident
            if wanted and not keep:
                fallback.add(ident)               # nothing usable was stored: the defaults stay
                continue
            final[ident] = keep
        for ident in ids:
            if ident in overrides and ident not in fallback:
                continue
            keep = []
            for key in self._defaults.get(ident, []):
                norm = normalize_key(key) or key
                owner = taken.get(norm)
                if owner is not None and owner != ident:
                    dropped.append((ident, key, f"default taken by {owner}"))
                    continue
                keep.append(key)
            final[ident] = keep
        return final, dropped

    def owner(self, key: Any, overrides: Mapping[str, list[str]] | None = None, *,
              exclude: str | None = None) -> str | None:
        """The id that has ``key`` under ``overrides`` (default: the applied ones), other than ``exclude``."""
        norm = normalize_key(key)
        if norm is None:
            return None
        final, _dropped = self.resolve(self._overrides if overrides is None else overrides)
        for ident, keys in final.items():
            if ident != exclude and norm in {normalize_key(k) for k in keys}:
                return ident
        return None

    def refusal(self, ident: str, key: Any, overrides: Mapping[str, list[str]] | None = None) -> str | None:
        """Why ``ident`` cannot have ``key`` (localized), ``None`` when it can (see the module docstring)."""
        try:
            count = _sequence(key).count()
        except (TypeError, ValueError):
            count = 0
        norm = normalize_key(key)
        shown = native_text([norm]) if norm else str(key.toString(NATIVE) if isinstance(key, QKeySequence) else key)
        if norm is None:
            if count > 1:
                return t("gui.keymap.multi", keys=shown)
            return t("gui.keymap.invalid", keys=shown)
        own = {normalize_key(k) for k in self.defaults(ident)}
        if norm not in own:
            fk = _FIXED.get(norm)
            if fk is not None:
                return t("gui.keymap.fixed_conflict", keys=shown, what=t(fk.what))
            menu = self._mnemonics().get(norm)
            if menu is not None:
                return t("gui.keymap.menu_conflict", keys=shown, menu=menu)
        other = self.owner(norm, overrides, exclude=ident)
        if other is not None:
            return t("gui.keymap.conflict", keys=shown, action=self.label(other), path=self.paths().get(other, "")
                     or "\N{EM DASH}")
        return None

    # ------------------------------------------------------------------ changing
    def reload(self) -> None:
        """Read the overrides from the GUI state and apply them (start; another window saved new keys)."""
        self._load()
        self.apply()

    def _load(self) -> None:
        raw = self._state.get(STATE_KEY) if self._state is not None else None
        self._overrides, self._unknown = {}, {}
        if raw is None:
            return
        if not isinstance(raw, dict):
            log.warning("ignoring the stored keyboard settings: not a mapping (%s)", type(raw).__name__)
            return
        known = set(self.actions())
        for key, value in raw.items():
            keys = _clean_list(value)
            if not isinstance(key, str) or keys is None:
                log.warning("ignoring the stored keys of %r: %r", key, value)
                continue
            if key in known:
                self._overrides[key] = keys
            else:
                self._unknown[key] = keys

    def apply(self) -> None:
        """Give every configurable action its keys (overrides over defaults; see :meth:`resolve`)."""
        actions = self.actions()
        final, dropped = self.resolve(self._overrides)
        for ident, key, why in dropped:
            log.warning("keyboard settings: %s loses %s (%s)", ident, key, why)
        for ident, keys in final.items():
            action = actions.get(ident)
            if action is None:
                continue
            now = [s.toString(PORTABLE) for s in action.shortcuts() if not s.isEmpty()]
            if now != keys:
                try:
                    action.setShortcuts([QKeySequence(k, PORTABLE) for k in keys])
                except RuntimeError:  # pragma: no cover - deleted meanwhile
                    continue
        self.changed.emit()

    def check(self, ident: str, keys: Iterable[Any], overrides: Mapping[str, list[str]] | None = None) -> str | None:
        """The first refusal of giving ``ident`` the keys ``keys`` (``None``: all can be used)."""
        seen: set[str] = set()
        for key in keys:
            why = self.refusal(ident, key, overrides)
            if why is not None:
                return why
            norm = normalize_key(key)
            if norm in seen:
                return t("gui.keymap.twice", keys=native_text([norm]))
            seen.add(norm or "")
        return None

    def set_keys(self, ident: str, keys: Iterable[Any] | None) -> str | None:
        """Give ``ident`` the keys ``keys`` (``None``: its defaults back), applied at once and stored. Returns the
        refusal (nothing changed) or ``None``."""
        if ident not in self.actions():
            return t("gui.keymap.unknown", action=ident)
        if keys is None:
            return self.reset(ident)
        keys = list(keys)
        others = {k: v for k, v in self._overrides.items() if k != ident}
        why = self.check(ident, keys, others)
        if why is not None:
            return why
        overrides = dict(self._overrides)
        normalized = [normalize_key(k) or str(k) for k in keys]
        if normalized == [normalize_key(k) or k for k in self.defaults(ident)]:
            overrides.pop(ident, None)
        else:
            overrides[ident] = normalized
        self._commit(overrides)
        return None

    def reset(self, ident: str) -> str | None:
        """``ident`` gets its default keys back (refused while another action's key override uses one of them)."""
        overrides = {k: v for k, v in self._overrides.items() if k != ident}
        why = self.reset_refusal(ident, overrides)
        if why is not None:
            return why
        self._commit(overrides)
        return None

    def reset_refusal(self, ident: str, overrides: Mapping[str, list[str]]) -> str | None:
        """Why the defaults of ``ident`` cannot come back under ``overrides`` (another action's override has one)."""
        final, _dropped = self.resolve(overrides)
        lost = [k for k in self.defaults(ident) if k not in final.get(ident, [])]
        if not lost:
            return None
        other = self.owner(lost[0], overrides, exclude=ident)
        return t("gui.keymap.reset_conflict", keys=native_text(lost[:1]),
                 action=self.label(other) if other is not None else "?")

    def reset_all(self) -> None:
        """Every action gets its default keys back."""
        self._commit({})

    def set_overrides(self, overrides: Mapping[str, list[str]]) -> None:
        """Replace all overrides (the settings page's "Saqlash"): applied at once and stored."""
        clean = {}
        for k, v in overrides.items():
            keys = _clean_list(v)
            if isinstance(k, str) and keys is not None:
                clean[k] = keys
        self._commit(clean)

    def _commit(self, overrides: dict[str, list[str]]) -> None:
        self._overrides = {k: list(v) for k, v in overrides.items()}
        self.apply()
        stored = {**self._unknown, **self._overrides}
        if self._state is not None:
            try:
                self._state.set(STATE_KEY, dict(sorted(stored.items())) or None)
            except (TypeError, ValueError):
                log.exception("could not store the keyboard settings")
                return
        if callable(self._save):
            try:
                self._save()
            except Exception:
                log.debug("saving the GUI state failed", exc_info=True)


def keymap_of(widget: Any) -> KeyMap | None:
    """The :class:`KeyMap` of the main window ``widget`` belongs to (its ``keymap``), if any."""
    seen = 0
    w = widget
    while w is not None and seen < 64:
        keymap = getattr(w, "keymap", None)
        if isinstance(keymap, KeyMap):
            return keymap
        w = w.parentWidget() if isinstance(w, QWidget) else None
        seen += 1
    return None


# ==================================================================================
# the settings page
# ==================================================================================
class KeymapPage(QWidget):
    """The "Klaviatura" page (see the module docstring). ``changed`` is emitted when the working copy changed."""

    changed = Signal()
    COL_ACTION, COL_PATH, COL_DEFAULT, COL_CURRENT, COL_RESET = range(5)

    def __init__(self, keymap: KeyMap | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("keymap_page")
        self.keymap = keymap
        self._working: dict[str, list[str]] = keymap.overrides() if keymap is not None else {}
        self._rows: list[KeyRow] = keymap.rows() if keymap is not None else []
        self._row_of = {row.id: i for i, row in enumerate(self._rows)}
        self._search: dict[str, str] = {}
        self._shown_key: str | None = None            # the key the recorder shows (normalized)
        layout = QVBoxLayout(self)
        self.filter_edit = QLineEdit(self)
        self.filter_edit.setObjectName("keymap_filter")
        self.filter_edit.setPlaceholderText(t("gui.keymap.filter.placeholder"))
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self.set_filter)
        layout.addWidget(self.filter_edit)
        self.table = QTableWidget(len(self._rows), 5, self)
        self.table.setObjectName("keymap_table")
        self.table.setHorizontalHeaderLabels([t("gui.keymap.col.action"), t("gui.keymap.col.path"),
                                              t("gui.keymap.col.default"), t("gui.keymap.col.current"), ""])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        row_height = self.fontMetrics().height() + 8
        self.table.verticalHeader().setDefaultSectionSize(row_height)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(self.COL_ACTION, QHeaderView.ResizeMode.Stretch)
        for col in (self.COL_PATH, self.COL_DEFAULT, self.COL_CURRENT, self.COL_RESET):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 3)
        record = QHBoxLayout()
        self.record_label = QLabel(t("gui.keymap.record.label"), self)
        record.addWidget(self.record_label)
        self.recorder = QKeySequenceEdit(self)
        self.recorder.setObjectName("keymap_recorder")
        self.recorder.setMaximumSequenceLength(1)
        self.recorder.setToolTip(t("gui.keymap.record.tip"))
        self.recorder.editingFinished.connect(self._recorded)
        record.addWidget(self.recorder, 1)
        self.none_button = QPushButton(t("gui.keymap.none"), self)
        self.none_button.setObjectName("keymap_none")
        self.none_button.setToolTip(t("gui.keymap.none.tip"))
        self.none_button.setAutoDefault(False)
        self.none_button.clicked.connect(lambda: self._clear_selected())
        record.addWidget(self.none_button)
        self.reset_all_button = QPushButton(t("gui.keymap.reset_all"), self)
        self.reset_all_button.setObjectName("keymap_reset_all")
        self.reset_all_button.setToolTip(t("gui.keymap.reset_all.tip"))
        self.reset_all_button.setAutoDefault(False)
        self.reset_all_button.clicked.connect(lambda: self.reset_all())
        record.addWidget(self.reset_all_button)
        layout.addLayout(record)
        self.message = QLabel(self)
        self.message.setObjectName("keymap_message")
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.message)
        self.fixed_label = QLabel(t("gui.keymap.fixed.title"), self)
        bold = QFont(self.fixed_label.font())
        bold.setBold(True)
        self.fixed_label.setFont(bold)
        self.fixed_label.setToolTip(t("gui.keymap.fixed.note"))
        layout.addWidget(self.fixed_label)
        self.fixed_table = QTableWidget(len(FIXED_KEYS), 2, self)
        self.fixed_table.setObjectName("keymap_fixed")
        self.fixed_table.setHorizontalHeaderLabels([t("gui.keymap.fixed.col.keys"), t("gui.keymap.fixed.col.what")])
        self.fixed_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.fixed_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.fixed_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.fixed_table.verticalHeader().setVisible(False)
        self.fixed_table.verticalHeader().setDefaultSectionSize(row_height)
        self.fixed_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.fixed_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.fixed_table.setToolTip(t("gui.keymap.fixed.note"))
        for i, fk in enumerate(FIXED_KEYS):
            self.fixed_table.setItem(i, 0, QTableWidgetItem(fk.shown))
            self.fixed_table.setItem(i, 1, QTableWidgetItem(t(fk.what)))
        self.fixed_table.setMaximumHeight(150)
        layout.addWidget(self.fixed_table, 1)
        self._fill()
        self.table.itemSelectionChanged.connect(self._selection_changed)
        if keymap is None:
            for w in (self.filter_edit, self.table, self.recorder, self.none_button, self.reset_all_button):
                w.setEnabled(False)
            self._say(t("gui.keymap.unavailable"), "muted")
        else:
            self._say(t("gui.keymap.hint"), "muted")
        self._selection_changed()
        theme.notifier().theme_changed.connect(self._restyle)

    # ------------------------------------------------------------------ working copy
    def overrides(self) -> dict[str, list[str]]:
        """The working copy of the overrides (applied by :meth:`apply`)."""
        return {k: list(v) for k, v in self._working.items()}

    def is_dirty(self) -> bool:
        return self.keymap is not None and self._working != self.keymap.overrides()

    def current_keys(self, ident: str) -> list[str]:
        """The keys ``ident`` gets with the working copy."""
        if self.keymap is None:
            return []
        final, _dropped = self.keymap.resolve(self._working)
        return list(final.get(ident, []))

    def assign(self, ident: str, key: Any) -> str | None:
        """``ident`` gets the one key ``key`` (its other keys go); refused (with the reason shown) when it cannot."""
        if self.keymap is None or ident not in self._row_of:
            return t("gui.keymap.unavailable")
        others = {k: v for k, v in self._working.items() if k != ident}
        why = self.keymap.refusal(ident, key, others)
        if why is not None:
            self._say(why, "danger")
            self._sync_recorder()
            return why
        norm = normalize_key(key) or str(key)
        working = dict(self._working)
        defaults = [normalize_key(k) or k for k in self.keymap.defaults(ident)]
        if [norm] == defaults:
            working.pop(ident, None)
        else:
            working[ident] = [norm]
        self._set_working(working)
        self._say(t("gui.keymap.assigned", action=self._rows[self._row_of[ident]].text, keys=native_text([norm])),
                  "ok")
        return None

    def clear_keys(self, ident: str) -> str | None:
        """``ident`` gets no key at all."""
        if self.keymap is None or ident not in self._row_of:
            return t("gui.keymap.unavailable")
        working = dict(self._working)
        if self.keymap.defaults(ident):
            working[ident] = []
        else:
            working.pop(ident, None)
        self._set_working(working)
        self._say(t("gui.keymap.cleared", action=self._rows[self._row_of[ident]].text), "muted")
        return None

    def reset(self, ident: str) -> str | None:
        """"Asl holiga" of a row: the default keys back -- refused while another command's new key is one of them."""
        if self.keymap is None or ident not in self._row_of:
            return t("gui.keymap.unavailable")
        working = {k: v for k, v in self._working.items() if k != ident}
        why = self.keymap.reset_refusal(ident, working)
        if why is not None:
            self._say(why, "danger")
            return why
        self._set_working(working)
        keys = self.keymap.defaults(ident)
        self._say(t("gui.keymap.reset_done", action=self._rows[self._row_of[ident]].text,
                    keys=native_text(keys) if keys else t("gui.keymap.no_keys")), "muted")
        return None

    def reset_all(self) -> None:
        """"Hammasini asl holiga": every command's defaults (applied with "Saqlash")."""
        if self.keymap is None:
            return
        self._set_working({})
        self._say(t("gui.keymap.all_reset"), "muted")

    def apply(self) -> bool:
        """Give the window the working copy (the settings dialog's "Saqlash"); True when something changed."""
        if self.keymap is None or not self.is_dirty():
            return False
        self.keymap.set_overrides(self._working)
        self._working = self.keymap.overrides()
        return True

    def _set_working(self, working: dict[str, list[str]]) -> None:
        self._working = working
        self._fill()
        self._sync_recorder()
        self.changed.emit()

    # ------------------------------------------------------------------ table
    def ids(self) -> list[str]:
        return [row.id for row in self._rows]

    def visible_ids(self) -> list[str]:
        return [row.id for i, row in enumerate(self._rows) if not self.table.isRowHidden(i)]

    def select(self, ident: str) -> None:
        row = self._row_of.get(ident)
        if row is not None:
            self.table.selectRow(row)
            self.table.scrollToItem(self.table.item(row, 0))

    def selected_id(self) -> str | None:
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        if len(rows) != 1:
            return None
        row = rows.pop()
        return self._rows[row].id if 0 <= row < len(self._rows) else None

    def reset_button(self, ident: str) -> QToolButton | None:
        row = self._row_of.get(ident)
        widget = self.table.cellWidget(row, self.COL_RESET) if row is not None else None
        return widget if isinstance(widget, QToolButton) else None

    def set_filter(self, text: str) -> None:
        """Show the rows whose command, menu path or keys contain ``text`` (folded like the palette)."""
        query = fold_text(str(text or "").strip())
        for i, row in enumerate(self._rows):
            if not query:
                self.table.setRowHidden(i, False)
                continue
            hay = self._search.get(row.id, "")
            self.table.setRowHidden(i, query not in hay)

    def _fill(self) -> None:
        if self.keymap is None:
            return
        final, _dropped = self.keymap.resolve(self._working)
        none = t("gui.keymap.no_keys")
        for i, row in enumerate(self._rows):
            current = final.get(row.id, [])
            row.current = list(current)
            changed = row.id in self._working
            cells = (row.text, row.path or "\N{EM DASH}",
                     native_text(row.default, keyboard_only=True) if row.default else none,
                     native_text(current, keyboard_only=True) if current else none)
            for col, text in enumerate(cells):
                item = self.table.item(i, col)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(i, col, item)
                if item.text() != text:
                    item.setText(text)
                if col == self.COL_CURRENT:
                    font = QFont(item.font())
                    font.setBold(changed)
                    item.setFont(font)
                    item.setToolTip(t("gui.keymap.changed.tip", keys=cells[self.COL_DEFAULT]) if changed else "")
            button = self.table.cellWidget(i, self.COL_RESET)
            if not isinstance(button, QToolButton):
                button = QToolButton(self.table)
                button.setText(t("gui.keymap.reset"))
                button.setToolTip(t("gui.keymap.reset.tip"))
                button.setAutoRaise(True)
                button.setObjectName(f"keymap_reset_{row.id}")
                button.clicked.connect(lambda _checked=False, k=row.id: self.reset(k))
                self.table.setCellWidget(i, self.COL_RESET, button)
            button.setEnabled(changed)
            self._search[row.id] = fold_text(" ".join((row.text, row.path, native_text(row.default),
                                                       native_text(current), " ".join(row.default),
                                                       " ".join(current))))

    def _selection_changed(self) -> None:
        ident = self.selected_id()
        on = self.keymap is not None and ident is not None
        self.recorder.setEnabled(on)
        self.none_button.setEnabled(on)
        self._sync_recorder()

    def _sync_recorder(self) -> None:
        ident = self.selected_id()
        keys = self.current_keys(ident) if ident is not None else []
        self._shown_key = normalize_key(keys[0]) if keys else None
        self.recorder.blockSignals(True)
        self.recorder.setKeySequence(QKeySequence(keys[0], PORTABLE) if keys else QKeySequence())
        self.recorder.blockSignals(False)

    def _recorded(self) -> None:
        ident = self.selected_id()
        seq = self.recorder.keySequence()
        if ident is None or seq.isEmpty():
            return
        if normalize_key(seq) == self._shown_key:
            return             # the key it showed (e.g. the first of two): nothing new, the other keys stay
        self.assign(ident, seq)

    def _clear_selected(self) -> None:
        ident = self.selected_id()
        if ident is not None:
            self.clear_keys(ident)

    def _say(self, text: str, role: str) -> None:
        self.message.setText(text)
        self.message.setProperty("role", role)
        self.message.setStyleSheet(f"color: {theme.color(role).name()};")

    def _restyle(self) -> None:
        role = str(self.message.property("role") or "muted")
        self.message.setStyleSheet(f"color: {theme.color(role).name()};")
