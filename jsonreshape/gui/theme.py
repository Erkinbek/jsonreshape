"""Colors, type badges, fonts and the application palette (docs/ARCHITECTURE.md §11.1).

The palette follows the user's prototype (``json-tuzilma.html``): a cool gray UI with a
blue accent and one color per JSON type, in a light and a dark scheme.

* :func:`colors` returns the pure color table of a scheme (no ``QApplication`` needed);
* :func:`color` resolves a role for the *effective* scheme (cached);
* :func:`apply_theme` installs the Fusion style plus a full :class:`QPalette` and follows
  the system scheme in ``"system"`` mode; every change emits ``notifier().theme_changed``;
* :func:`mono_font` / :func:`ui_font` build fonts (need a ``QGuiApplication``);
* :func:`role_style` fills ``$role`` references of a style sheet with the role colours; :func:`bind_style` sets
  such a style sheet on a widget and sets it again after every theme change (stage 6: no colour is hard-coded
  outside this module -- ``tests/test_theme_colours.py`` enforces it).

Roles (keys of :func:`colors`):

=================  ==============================================================
``bg``             window background            ``panel``  views / editors (Base)
``panel_alt``      alternate rows, buttons      ``line``   separators, borders
``guide``          tree indent guides           ``fg``     normal text
``muted``          secondary text               ``faint``  placeholders, tertiary
``accent``         focus, links, highlight      ``accent_ink``  text on ``accent``
``accent_soft``    soft accent tint             ``hover``  hovered row
``selection``      selected rows (focused)      ``selection_inactive``  (unfocused)
``warn`` ``danger`` ``ok``  info bars / states  ``error`` (= danger) ``link`` (= accent)
``key``            object keys                  ``object array string number bool null``
``match``          search hit background        ``current_match``  the current hit
``badge_text``     glyph on a *filled* badge (type badges are tinted, see below)
``diff_added``     diff row tints: added        ``diff_removed``  removed
``diff_changed``   changed                      ``diff_moved``  moved (array key matching)
``danger_soft``    background of a field in error (``fg`` text; quick find "not found")
=================  ==============================================================

``boolean`` is accepted as an alias of ``bool`` everywhere.
"""

from __future__ import annotations

import logging
import re

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette

log = logging.getLogger(__name__)

SCHEMES: tuple[str, ...] = ("light", "dark")
MODES: tuple[str, ...] = ("system", "light", "dark")

# ---------------------------------------------------------------------------------
# color tables (hex; the prototype's CSS variables + derived roles)
# ---------------------------------------------------------------------------------
_TABLE: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#f2f4f7", "panel": "#ffffff", "panel_alt": "#f6f8fb", "line": "#dce1e9",
        "guide": "#d3dae5", "fg": "#17202c", "muted": "#5b6677", "faint": "#8993a4",
        "accent": "#2a57c4", "accent_ink": "#ffffff", "accent_soft": "#e6ecfb",
        "hover": "#eef2f9", "selection": "#d4ddf3", "selection_inactive": "#e8ecf4",
        "warn": "#9a5a08", "danger": "#b23434", "ok": "#2b7649",
        "object": "#4a53b5", "array": "#a0600f", "string": "#2b7649", "number": "#1e68a3",
        "bool": "#8344ad", "null": "#77808f",
        "key": "#2c3a56", "match": "#ffe48f", "current_match": "#ffbb4d",
        "badge_text": "#ffffff",
        # row tints of the structural diff (text in any type color stays readable on them)
        "diff_added": "#dcf3e2", "diff_removed": "#fbe2e2", "diff_changed": "#fcefcf", "diff_moved": "#d8eff8",
        "danger_soft": "#f7d4d4",
    },
    "dark": {
        "bg": "#0e1117", "panel": "#151a22", "panel_alt": "#1a2029", "line": "#29303c",
        "guide": "#2f3846", "fg": "#e3e7ef", "muted": "#9aa4b5", "faint": "#6d7889",
        "accent": "#7e9cff", "accent_ink": "#0e1117", "accent_soft": "#1d2745",
        "hover": "#1e2531", "selection": "#323e60", "selection_inactive": "#252e43",
        "warn": "#e2a45a", "danger": "#f07a7a", "ok": "#72cf98",
        "object": "#a0a8ff", "array": "#e2a45a", "string": "#72cf98", "number": "#6db5ef",
        "bool": "#c79cf0", "null": "#8b94a5",
        "key": "#c3cee4", "match": "#5e4a12", "current_match": "#8a4b00",
        "badge_text": "#0e1117",
        # row tints of the structural diff (text in any type color stays readable on them)
        "diff_added": "#17382a", "diff_removed": "#43222a", "diff_changed": "#3b3016", "diff_moved": "#14344a",
        "danger_soft": "#4d2229",
    },
}
_ALIASES = {"error": "danger", "link": "accent", "boolean": "bool"}

ROLES: tuple[str, ...] = tuple(_TABLE["light"]) + tuple(_ALIASES)

TYPES: tuple[str, ...] = ("object", "array", "string", "number", "bool", "null")

# Type badges: short glyphs readable at small sizes (the prototype's TYPE_GLYPH).
BADGES: dict[str, str] = {
    "object": "{}", "array": "[]", "string": "abc", "number": "123", "bool": "t/f", "null": "null",
}
BADGES["boolean"] = BADGES["bool"]

# opacity of the tinted badge background (prototype: color-mix 12 %; a bit more in dark)
_BADGE_ALPHA = {"light": 34, "dark": 46}

# ---------------------------------------------------------------------------------
# change notification + state
# ---------------------------------------------------------------------------------


class ThemeNotifier(QObject):
    """Module-level singleton; ``theme_changed`` fires after the palette/colors changed."""

    theme_changed = Signal()


_notifier: ThemeNotifier | None = None
_mode: str = "system"
_cache: dict[str, QColor] | None = None     # colors of the effective scheme
_cache_scheme: str | None = None
_applied_scheme: str | None = None          # scheme of the last installed palette
_system_palette: QPalette | None = None     # the palette before apply_theme (fallback detection)
_hints_connected = False
_applying = False
_mono_family: str | None = None


def notifier() -> ThemeNotifier:
    """The process-wide :class:`ThemeNotifier` (created on first use)."""
    global _notifier
    if _notifier is None:
        _notifier = ThemeNotifier()
    return _notifier


def mode() -> str:
    """The configured mode (``system`` | ``light`` | ``dark``)."""
    return _mode


def _normalize_role(role: str) -> str:
    return _ALIASES.get(role, role)


def colors(scheme: str) -> dict[str, QColor]:
    """All role colors of ``scheme`` (``light`` | ``dark``), aliases included. Fresh objects."""
    if scheme not in _TABLE:
        raise ValueError(f"unknown color scheme: {scheme!r}")
    table = _TABLE[scheme]
    out = {name: QColor(value) for name, value in table.items()}
    for alias, target in _ALIASES.items():
        out[alias] = QColor(table[target])
    return out


def _palette_is_dark(pal: QPalette) -> bool:
    window = pal.color(QPalette.ColorRole.Window)
    text = pal.color(QPalette.ColorRole.WindowText)
    return window.lightness() < text.lightness()


def system_scheme() -> str:
    """The scheme the platform asks for (``light`` when unknown or without an application)."""
    app = QGuiApplication.instance()
    if app is None:
        return "light"
    try:
        scheme = QGuiApplication.styleHints().colorScheme()
    except (AttributeError, RuntimeError):  # pragma: no cover - Qt < 6.5
        scheme = None
    if scheme == Qt.ColorScheme.Dark:
        return "dark"
    if scheme == Qt.ColorScheme.Light:
        return "light"
    pal = _system_palette if _system_palette is not None else QGuiApplication.palette()
    return "dark" if _palette_is_dark(pal) else "light"


def current_scheme() -> str:
    """The effective scheme: the forced mode, else the system scheme."""
    if _mode in ("light", "dark"):
        return _mode
    return system_scheme()


def _active() -> dict[str, QColor]:
    global _cache, _cache_scheme
    if _cache is None:
        _cache_scheme = current_scheme()
        _cache = colors(_cache_scheme)
    return _cache


def invalidate() -> None:
    """Forget the cached colors (the next :func:`color` re-reads the effective scheme)."""
    global _cache, _cache_scheme
    _cache = None
    _cache_scheme = None


def color(role: str) -> QColor:
    """Color of ``role`` in the effective scheme (cached; unknown roles -> ``fg``).

    The returned object is shared: copy it (``QColor(c)``) before modifying it.
    """
    table = _active()
    found = table.get(role)
    if found is None:
        found = table.get(_normalize_role(role)) or table["fg"]
    return found


def is_dark() -> bool:
    return (_cache_scheme if _cache is not None else current_scheme()) == "dark"


def badge_color(kind: str | None) -> QColor:
    """Color of the type badge ``kind`` (``object`` ... ``null``); ``muted`` for unknown."""
    if not kind:
        return color("muted")
    role = _normalize_role(kind)
    return color(role) if role in TYPES else color("muted")


def badge_background(kind: str | None) -> QColor:
    """Tinted (semi-transparent) badge background: the type color at low opacity."""
    tint = QColor(badge_color(kind))
    tint.setAlpha(_BADGE_ALPHA["dark" if is_dark() else "light"])
    return tint


def badge_glyph(kind: str | None) -> str:
    return BADGES.get(kind or "", "?")


# ---------------------------------------------------------------------------------
# style sheets with role colours
# ---------------------------------------------------------------------------------
_ROLE_REF = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
_BINDING_NAME = "jsonreshape-theme-style"


def role_style(template: str) -> str:
    """``template`` with every ``$role`` replaced by that role's colour (``#rrggbb``) in the effective scheme:
    ``role_style("color: $danger;")``. ``ValueError`` for an unknown role."""

    def sub(match: re.Match[str]) -> str:
        role = match.group(1)
        if role not in ROLES:
            raise ValueError(f"unknown colour role: {role!r}")
        return color(role).name()

    return _ROLE_REF.sub(sub, template)


class _StyleBinding(QObject):
    """Child of a widget: sets its style sheet from a ``$role`` template, again after every theme change (the
    connection ends with the widget)."""

    def __init__(self, widget: QObject, template: str) -> None:
        super().__init__(widget)
        self.setObjectName(_BINDING_NAME)
        self.template = template
        notifier().theme_changed.connect(self.apply)

    @Slot()
    def apply(self) -> None:
        widget = self.parent()
        if widget is not None:
            widget.setStyleSheet(role_style(self.template))


def bind_style(widget: QObject, template: str) -> None:
    """``widget.setStyleSheet(role_style(template))`` now and after every theme change, until the widget is
    destroyed or the next call for it; ``template == ""`` clears the style sheet and ends the binding."""
    binding = widget.findChild(QObject, _BINDING_NAME, Qt.FindChildOption.FindDirectChildrenOnly)
    if not isinstance(binding, _StyleBinding):
        binding = None
    if not template:
        if binding is not None:
            notifier().theme_changed.disconnect(binding.apply)
            binding.setParent(None)
            binding.deleteLater()
        widget.setStyleSheet("")
        return
    if binding is None:
        binding = _StyleBinding(widget, template)
    else:
        binding.template = template
    binding.apply()


# ---------------------------------------------------------------------------------
# palette / application theme
# ---------------------------------------------------------------------------------
_PALETTE_SPEC: dict[str, dict[str, tuple[str, str | None]]] = {
    # role: (normal color, disabled color or None)
    "light": {
        "Window": ("#f2f4f7", None), "WindowText": ("#17202c", "#8993a4"),
        "Base": ("#ffffff", "#f6f8fb"), "AlternateBase": ("#f6f8fb", None),
        "ToolTipBase": ("#ffffff", None), "ToolTipText": ("#17202c", None),
        "PlaceholderText": ("#8993a4", None), "Text": ("#17202c", "#8993a4"),
        "Button": ("#f6f8fb", "#eef1f5"), "ButtonText": ("#17202c", "#8993a4"),
        "BrightText": ("#b23434", None),
        "Light": ("#ffffff", None), "Midlight": ("#eef1f5", None), "Mid": ("#c5ccd8", None),
        "Dark": ("#a3acbb", None), "Shadow": ("#6b7686", None),
        "Highlight": ("#2a57c4", "#c5ccd8"), "HighlightedText": ("#ffffff", "#5b6677"),
        "Link": ("#2a57c4", None), "LinkVisited": ("#4a53b5", None), "Accent": ("#2a57c4", "#c5ccd8"),
    },
    "dark": {
        "Window": ("#0e1117", None), "WindowText": ("#e3e7ef", "#6d7889"),
        "Base": ("#151a22", "#11151b"), "AlternateBase": ("#1a2029", None),
        "ToolTipBase": ("#1a2029", None), "ToolTipText": ("#e3e7ef", None),
        "PlaceholderText": ("#6d7889", None), "Text": ("#e3e7ef", "#6d7889"),
        "Button": ("#1a2029", "#151a22"), "ButtonText": ("#e3e7ef", "#6d7889"),
        "BrightText": ("#f07a7a", None),
        "Light": ("#343c4a", None), "Midlight": ("#262d38", None), "Mid": ("#1f252f", None),
        "Dark": ("#0a0d12", None), "Shadow": ("#000000", None),
        "Highlight": ("#7e9cff", "#29303c"), "HighlightedText": ("#0e1117", "#9aa4b5"),
        "Link": ("#7e9cff", None), "LinkVisited": ("#a0a8ff", None), "Accent": ("#7e9cff", "#29303c"),
    },
}


def build_palette(scheme: str) -> QPalette:
    """A complete :class:`QPalette` (all groups) for ``scheme``."""
    if scheme not in _PALETTE_SPEC:
        raise ValueError(f"unknown color scheme: {scheme!r}")
    pal = QPalette()
    roles = QPalette.ColorRole
    disabled = QPalette.ColorGroup.Disabled
    for name, (normal, dis) in _PALETTE_SPEC[scheme].items():
        role = getattr(roles, name, None)
        if role is None:  # pragma: no cover - e.g. Accent on an old Qt
            continue
        pal.setColor(role, QColor(normal))
        if dis is not None:
            pal.setColor(disabled, role, QColor(dis))
    return pal


def _install(app, scheme: str) -> None:
    global _applied_scheme
    from PySide6.QtWidgets import QToolTip  # local: theme data must not need QtWidgets

    pal = build_palette(scheme)
    app.setPalette(pal)
    QToolTip.setPalette(pal)
    _applied_scheme = scheme
    invalidate()


def apply_theme(app, mode: str = "system") -> None:
    """Install Fusion + the palette of ``mode`` (``system`` | ``light`` | ``dark``) on ``app``.

    In ``system`` mode the palette follows ``styleHints().colorSchemeChanged``. Always emits
    ``notifier().theme_changed`` (after the cached colors were invalidated).
    """
    global _mode, _system_palette, _hints_connected, _applying
    mode = (mode or "system").strip().lower()
    if mode not in MODES:
        log.warning("unknown theme mode %r, using 'system'", mode)
        mode = "system"
    if _system_palette is None:
        _system_palette = QPalette(app.palette())
    _applying = True
    try:
        _mode = mode
        if app.style() is None or app.style().name().lower() != "fusion":
            app.setStyle("Fusion")
        hints = app.styleHints()
        try:  # ask the platform for the scheme too (dark title bars on Windows); Qt >= 6.8
            if mode == "system":
                hints.unsetColorScheme()
            else:
                hints.setColorScheme(Qt.ColorScheme.Dark if mode == "dark" else Qt.ColorScheme.Light)
        except (AttributeError, RuntimeError):  # pragma: no cover - older Qt
            pass
        if not _hints_connected:
            hints.colorSchemeChanged.connect(_on_system_scheme_changed)
            _hints_connected = True
        _install(app, current_scheme())
    finally:
        _applying = False
    notifier().theme_changed.emit()


def _on_system_scheme_changed(*_args) -> None:
    if _applying or _mode != "system":
        return
    app = QGuiApplication.instance()
    if app is None:
        return
    scheme = current_scheme()
    if scheme == _applied_scheme:
        return
    _install(app, scheme)
    notifier().theme_changed.emit()


def _reset_for_tests() -> None:
    """Forget the mode and caches (tests only)."""
    global _mode, _applied_scheme
    _mode = "system"
    _applied_scheme = None
    invalidate()


# ---------------------------------------------------------------------------------
# fonts
# ---------------------------------------------------------------------------------
MONO_FAMILIES: tuple[str, ...] = ("Cascadia Mono", "Consolas")


def _default_point_size() -> float:
    size = QGuiApplication.font().pointSizeF() if QGuiApplication.instance() is not None else -1.0
    return size if size > 0 else 9.0


def default_mono_family() -> str:
    """First installed family of :data:`MONO_FAMILIES`, else the system fixed font."""
    global _mono_family
    if _mono_family is None:
        installed = set(QFontDatabase.families())
        for family in MONO_FAMILIES:
            if family in installed:
                _mono_family = family
                break
        else:
            _mono_family = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
    return _mono_family


def mono_font(family: str | None = None, point_size: float | None = None) -> QFont:
    """The monospace font for data (tree, table, text, raw views).

    Default family: :func:`default_mono_family`; default size: the UI font size, at least
    10 pt (monospace faces look smaller than the UI font).
    """
    font = QFont(family or default_mono_family())
    font.setStyleHint(QFont.StyleHint.Monospace, QFont.StyleStrategy.PreferDefault)
    font.setFixedPitch(True)
    if point_size is None or point_size <= 0:
        point_size = max(_default_point_size(), 10.0)
    font.setPointSizeF(float(point_size))
    return font


def ui_font(point_size: float | None = None) -> QFont:
    """The application UI font, optionally with another point size."""
    font = QFont(QGuiApplication.font()) if QGuiApplication.instance() is not None else QFont()
    if point_size is not None and point_size > 0:
        font.setPointSizeF(float(point_size))
    return font
