"""Colours come from ``gui/theme.py`` only (docs/ARCHITECTURE.md §15.0 "Themes": the stage-6 dark-mode audit).

* Static scan of ``jsonreshape/gui/**/*.py`` (``theme.py`` excepted): no hard-coded colour -- ``QColor("#…")``,
  ``QColor(r, g, b)`` with constants, ``QColor.fromRgb(…)`` constants, ``"#rrggbb"`` anywhere in code (stylesheets,
  HTML, fallback tables), ``rgb()`` / named CSS colours in stylesheets, ``Qt.GlobalColor.*`` (``transparent``
  excepted: it clears pixmaps). Docstrings are not code. :data:`ALLOWED` lists the real exceptions, each with its
  reason; an entry nothing matches any more fails too.
* Contrast of the text roles on every tinted background the painting code uses -- the diff rows, the selected /
  hovered rows of the grids (computed by ``vgrid._Colors``) and of the structure builder (theme roles), the info
  bars (``InfoBar.background_color()``) and the type badges -- in the light and the dark scheme.
* A dark-mode smoke through the real window: every stage-3–5 window, dialog and panel opens (and paints) in dark
  mode without a Qt warning, and after a live theme switch no style sheet keeps a colour of the other scheme.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

import jsonreshape

PKG = Path(jsonreshape.__file__).resolve().parent
ROOT = PKG.parent
GUI = PKG / "gui"

# ==================================================================================================
# 1. static scan
# ==================================================================================================
HEX = r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?![0-9a-zA-Z_])"
CSS_NAMES = ("white", "black", "red", "green", "blue", "gray", "grey", "yellow", "orange", "purple", "pink",
             "brown", "cyan", "magenta", "silver", "navy", "maroon", "olive", "lime", "teal", "aqua", "fuchsia",
             "darkred", "darkgreen", "darkblue", "darkgray", "darkgrey", "lightgray", "lightgrey", "gold", "crimson")
_CSS_PROP = r"(?:color|background(?:-color)?|border(?:-(?:top|right|bottom|left))?(?:-color)?|outline(?:-color)?" \
            r"|selection-(?:background-)?color|gridline-color|alternate-background-color|fill|stroke)"
RE_FULL_HEX = re.compile(r"^\s*" + HEX + r"\s*$")
RE_CSS_HEX = re.compile(_CSS_PROP + r"\s*:\s*[^;{}\"'\x00]*?" + HEX, re.IGNORECASE)
RE_CSS_FUNC = re.compile(_CSS_PROP + r"\s*:\s*[^;{}\x00]*?\b(?:rgba?|hsla?|hsva?)\(\s*[0-9.]", re.IGNORECASE)
RE_CSS_NAME = re.compile(_CSS_PROP + r"\s*:\s*[^;{}\x00]*?\b(?:" + "|".join(CSS_NAMES) + r")\b(?![-\w(])",
                         re.IGNORECASE)
RE_ATTR_HEX = re.compile(r"\bcolor\s*=\s*[\"']?" + HEX, re.IGNORECASE)       # <font color="#...">
QCOLOR_FACTORIES = {"fromRgb", "fromRgba", "fromRgbF", "fromRgba64", "fromHsv", "fromHsvF", "fromHsl", "fromHslF",
                    "fromCmyk", "fromCmykF", "fromString"}
QCOLOR_SETTERS = {"setNamedColor", "setRgb", "setRgbF", "setRgba", "setHsv", "setHsvF", "setHsl", "setHslF"}
GLOBAL_COLORS = {"white", "black", "red", "darkRed", "green", "darkGreen", "blue", "darkBlue", "cyan", "darkCyan",
                 "magenta", "darkMagenta", "yellow", "darkYellow", "gray", "darkGray", "lightGray", "color0", "color1"}


@dataclass(frozen=True)
class Finding:
    path: str        # relative to the project root (posix)
    line: int
    scope: str       # qualified name of the enclosing function / class ("" = module level)
    text: str        # the source of the offending expression (or the string constant)
    what: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line} [{self.scope or '<module>'}] {self.what}: {self.text}"


@dataclass(frozen=True)
class Allowed:
    path: str
    scope: str
    texts: frozenset[str]
    reason: str


#: The real exceptions (path, enclosing scope, the exact expressions, why).
ALLOWED: tuple[Allowed, ...] = (
    Allowed("jsonreshape/gui/app.py", "make_app_icon",
            frozenset({'QColor("#2a57c4")', 'QColor("#ffffff")'}),
            "the application icon (window, taskbar, Explorer, the .exe resource): fixed brand colours that are the "
            "same in every theme -- the OS shows the icon outside the program's palette"),
    Allowed("jsonreshape/gui/widgets.py", "swatch_pixmap",
            frozenset({"QColor(255, 255, 255)", "QColor(204, 204, 204)"}),
            "the transparency checkerboard behind a translucent colour swatch of a value (smart values): white / "
            "light grey in every theme, as in every image editor -- it shows the value's alpha, not the UI"),
    Allowed("jsonreshape/gui/vgrid.py", "VirtualGrid._checker_pixmap.draw",
            frozenset({"QColor(255, 255, 255)", "QColor(204, 204, 204)"}),
            "the same transparency checkerboard, painted in the grid's colour-swatch cells"),
)


def gui_sources() -> list[Path]:
    return sorted(p for p in GUI.rglob("*.py") if "__pycache__" not in p.parts and p.name != "theme.py")


def _is_const_number(node: ast.AST) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        node = node.operand
    return isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool)


def _const_str(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _callee(node: ast.Call) -> tuple[str, str]:
    """(object name or "", attribute / function name) of a call."""
    f = node.func
    if isinstance(f, ast.Name):
        return "", f.id
    if isinstance(f, ast.Attribute):
        base = f.value
        name = base.id if isinstance(base, ast.Name) else base.attr if isinstance(base, ast.Attribute) else ""
        return name, f.attr
    return "", ""


def _string_problem(text: str) -> str | None:
    if RE_FULL_HEX.match(text):
        return "hex colour string"
    for rx, what in ((RE_CSS_HEX, "hex colour in a style sheet"), (RE_CSS_FUNC, "rgb()/hsl() colour in a style sheet"),
                     (RE_CSS_NAME, "named colour in a style sheet"), (RE_ATTR_HEX, "hex colour attribute")):
        if rx.search(text):
            return what
    return None


def scan_source(source: str, rel: str) -> list[Finding]:
    """Hard-coded colours in one module's source."""
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    docstrings: set[ast.AST] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and _const_str(node.value):
            docstrings.add(node.value)                     # docstrings and other bare strings: documentation

    def scope_of(node: ast.AST) -> str:
        names = []
        cur = parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(cur.name)
            cur = parents.get(cur)
        return ".".join(reversed(names))

    out: list[Finding] = []
    consumed: set[ast.AST] = set()                       # string arguments of a reported call

    def add(node: ast.AST, what: str) -> None:
        text = ast.get_source_segment(source, node) or ""
        out.append(Finding(rel, getattr(node, "lineno", 0), scope_of(node), " ".join(text.split()), what))
        if isinstance(node, ast.Call):
            consumed.update(a for a in node.args if _const_str(a))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            obj, name = _callee(node)
            args = list(node.args)
            if name == "QColor" and args:
                if any(_const_str(a) for a in args):
                    add(node, "QColor from a string constant")
                elif all(_is_const_number(a) for a in args):
                    add(node, "QColor from constant numbers")
            elif obj == "QColor" and name in QCOLOR_FACTORIES and args and \
                    all(_is_const_number(a) or _const_str(a) for a in args):
                add(node, f"QColor.{name} of constants")
            elif name in QCOLOR_SETTERS and args and all(_is_const_number(a) or _const_str(a) for a in args) \
                    and (name == "setNamedColor" or len(args) >= 3):
                add(node, f"{name} of constants")
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Attribute) and node.value.attr == "GlobalColor":
                if node.attr != "transparent":
                    add(node, "Qt.GlobalColor")
            elif isinstance(node.value, ast.Name) and node.value.id == "Qt" and node.attr in GLOBAL_COLORS:
                add(node, "Qt global colour")
        elif isinstance(node, ast.JoinedStr):
            text = "".join(v.value if _const_str(v) else "\x00" for v in node.values)
            what = _string_problem(text)
            if what:
                add(node, what)
        elif _const_str(node) and node not in docstrings and node not in consumed \
                and not isinstance(parents.get(node), ast.JoinedStr):
            what = _string_problem(node.value)
            if what:
                add(node, what)
    return out


def _allowed(f: Finding) -> Allowed | None:
    for entry in ALLOWED:
        if entry.path == f.path and entry.scope == f.scope and f.text in entry.texts:
            return entry
    return None


def all_findings() -> list[Finding]:
    found: list[Finding] = []
    for path in gui_sources():
        found += scan_source(path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix())
    return found


def test_no_hard_coded_colours_outside_theme() -> None:
    bad = [f for f in all_findings() if _allowed(f) is None]
    assert not bad, "hard-coded colours (use gui/theme.py roles):\n" + "\n".join(map(str, bad))


def test_every_allowed_exception_is_still_needed() -> None:
    found = all_findings()
    for entry in ALLOWED:
        assert entry.reason
        used = {f.text for f in found if f.path == entry.path and f.scope == entry.scope}
        assert entry.texts <= used, f"stale allow-list entry {entry.path} {entry.scope}: {sorted(entry.texts - used)}"


@pytest.mark.parametrize("source, what", [
    ('QColor("#b23434")', "QColor from a string constant"),
    ("QColor('red')", "QColor from a string constant"),
    ("QColor(255, 0, 0)", "QColor from constant numbers"),
    ("QColor(0xff0000)", "QColor from constant numbers"),
    ("QtGui.QColor(10, 20, 30, 40)", "QColor from constant numbers"),
    ("QColor.fromRgb(1, 2, 3)", "QColor.fromRgb of constants"),
    ("QColor.fromString('#fff')", "QColor.fromString of constants"),
    ("c.setNamedColor('#123456')", "setNamedColor of constants"),
    ("c.setRgb(1, 2, 3)", "setRgb of constants"),
    ("p.setPen(Qt.GlobalColor.black)", "Qt.GlobalColor"),
    ("QBrush(Qt.GlobalColor.darkGray)", "Qt.GlobalColor"),
    ("p.setPen(Qt.white)", "Qt global colour"),
    ('w.setStyleSheet("QLineEdit { background: #f5c6c6; color: #17202c; }")', "hex colour in a style sheet"),
    ('w.setStyleSheet(f"QLabel {{ color: {x}; border: 1px solid #ccc; }}")', "hex colour in a style sheet"),
    ('w.setStyleSheet("color: rgb(10, 20, 30);")', "rgb()/hsl() colour in a style sheet"),
    ('w.setStyleSheet("background-color: white")', "named colour in a style sheet"),
    ('w.setStyleSheet("border: 1px solid gray;")', "named colour in a style sheet"),
    ('FALLBACK = {"error": "#b23434"}', "hex colour string"),
    ('html = "<font color=#ff0000>x</font>"', "hex colour attribute"),
    ('html = "<span style=\'color:#ff0000\'>x</span>"', "hex colour in a style sheet"),
])
def test_scanner_finds(source: str, what: str) -> None:
    found = scan_source(source, "x.py")
    assert [f.what for f in found] == [what], found


@pytest.mark.parametrize("source", [
    '"""Docstring: QColor("#b23434"), color: #fff and Qt.GlobalColor.red are fine in prose."""',
    "def f():\n    '''color: red; QColor(\"#123456\")'''\n    return 1",
    "pm.fill(Qt.GlobalColor.transparent)",
    "QColor(theme.color('danger'))",
    "QColor(r, g, b, a)",
    "QColor(c.red(), c.green(), c.blue())",
    'w.setStyleSheet(f"color: {theme.color(role).name()};")',
    'w.setStyleSheet(f"background: rgba({c.red()},{c.green()},{c.blue()},{c.alpha()})")',
    'w.setStyleSheet("color: palette(bright-text);")',
    'w.setStyleSheet("font-weight: bold;")',
    'text = "#120 is a record, #0 too"',
    'text = "data[120].contact, $.data[120], /data/120, #120"',
    'label = "color: blueish-thing"',
    't("gui.colour.red")',
    'name = "border-radius: 3px"',
])
def test_scanner_allows(source: str) -> None:
    assert scan_source(source, "x.py") == []


# ==================================================================================================
# 2. contrast of the text roles on tinted backgrounds
# ==================================================================================================
#: Text roles drawn on the rows of the views (keys, values in their type colours, secondary texts, states, links).
TEXT_ROLES = ("fg", "key", "string", "number", "bool", "object", "array", "null", "muted", "danger", "warn", "ok",
              "accent")
#: Minimum contrast on a steady tint (a diff row, the current node's highlight, an info bar) -- the thresholds
#: ``test_gui_theme.test_colors_are_readable`` sets on the plain panel: a tint must not make text less readable.
STEADY = {"fg": 7.0, "key": 4.5, "null": 3.0}
STEADY_OTHER = 4.0
#: On a transient background (the selected row, the row under the mouse; the current row also gets the focus
#: outline): 0.5 lower.
TRANSIENT = {"fg": 7.0, "key": 4.5, "null": 2.5}
TRANSIENT_OTHER = 3.5
#: A type badge's glyph on its own translucent tint: on the panel / on a selected row.
BADGE_STEADY, BADGE_TRANSIENT = 3.0, 2.5


def _luminance(c) -> float:
    def ch(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * ch(c.redF()) + 0.7152 * ch(c.greenF()) + 0.0722 * ch(c.blueF())


def contrast(a, b) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _over(top, bottom):
    """``top`` (with its alpha) painted over the opaque ``bottom``."""
    from PySide6.QtGui import QColor

    a = top.alphaF()
    return QColor(round(top.red() * a + bottom.red() * (1 - a)), round(top.green() * a + bottom.green() * (1 - a)),
                  round(top.blue() * a + bottom.blue() * (1 - a)))


@pytest.fixture
def themed(qapp):
    """``use(scheme)`` applies the scheme; the application's palette, style and theme mode are restored."""
    from PySide6.QtGui import QPalette

    from jsonreshape.gui import theme

    palette = QPalette(qapp.palette())
    style = qapp.style().name()

    def use(scheme: str) -> None:
        theme.apply_theme(qapp, scheme)

    yield use
    theme._reset_for_tests()
    try:
        qapp.styleHints().unsetColorScheme()
    except AttributeError:  # pragma: no cover
        pass
    qapp.setStyle(style)
    qapp.setPalette(palette)
    theme.invalidate()


def tinted_backgrounds(scheme: str, qtbot) -> list[tuple[str, object, tuple[str, ...], bool]]:
    """``(name, colour, text roles drawn on it, transient)`` for every tinted background of the painting code."""
    from jsonreshape.gui import theme, vgrid
    from jsonreshape.gui.widgets import InfoBar

    c = theme.colors(scheme)
    grid = vgrid._Colors(theme.build_palette(scheme))       # what the virtual grids paint (tree, table, diff tree)
    out: list[tuple[str, object, tuple[str, ...], bool]] = []
    for role in ("diff_added", "diff_removed", "diff_changed", "diff_moved"):
        out.append((role, c[role], TEXT_ROLES, False))
        out.append((f"grid hover on {role}", vgrid._blend(c[role], grid.focus, vgrid.HOVER_TINT_BLEND), TEXT_ROLES,
                    True))
    out.append(("accent_soft (current node, drop target)", c["accent_soft"], TEXT_ROLES, False))
    out.append(("danger_soft (field in error)", c["danger_soft"], ("fg",), False))
    out.append(("grid selection", grid.sel, TEXT_ROLES, True))
    out.append(("grid selection (unfocused)", grid.sel_inactive, TEXT_ROLES, True))
    out.append(("grid hover", grid.hover, TEXT_ROLES, True))
    for role in ("selection", "selection_inactive", "hover"):        # the structure builder paints these roles
        out.append((role, c[role], TEXT_ROLES, True))
    for kind in InfoBar.KINDS:
        bar = InfoBar(kind, "x")
        qtbot.addWidget(bar)
        out.append((f"info bar {kind}", bar.background_color(), ("fg", "muted", "accent"), False))
    return out


@pytest.mark.gui
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_text_roles_are_readable_on_every_tint(scheme: str, themed, qtbot) -> None:
    from jsonreshape.gui import theme

    themed(scheme)
    c = theme.colors(scheme)
    problems = []
    backgrounds = tinted_backgrounds(scheme, qtbot)
    for name, bg, roles, transient in backgrounds:
        limits, other = (TRANSIENT, TRANSIENT_OTHER) if transient else (STEADY, STEADY_OTHER)
        for role in roles:
            value = contrast(c[role], bg)
            need = limits.get(role, other)
            if value < need:
                problems.append(f"{scheme}: {role} {c[role].name()} on {name} {bg.name()}: {value:.2f} < {need}")
    assert not problems, "\n".join(problems)
    # every tint is visible on the panel, and the dark scheme's tints really are dark
    for name, bg, _roles, _transient in backgrounds:
        assert bg.name() != c["panel"].name(), name
        assert (bg.lightness() < 128) == (scheme == "dark"), name


@pytest.mark.gui
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_type_badges_are_readable(scheme: str, themed) -> None:
    from jsonreshape.gui import theme, vgrid

    themed(scheme)
    c = theme.colors(scheme)
    grid = vgrid._Colors(theme.build_palette(scheme))
    for kind in theme.TYPES:
        glyph = theme.badge_color(kind)
        for bg, need in ((c["panel"], BADGE_STEADY), (c["panel_alt"], BADGE_STEADY), (grid.sel, BADGE_TRANSIENT),
                         (c["selection"], BADGE_TRANSIENT)):
            value = contrast(glyph, _over(theme.badge_background(kind), bg))
            assert value >= need, (scheme, kind, bg.name(), round(value, 2))


def test_danger_soft_in_both_schemes() -> None:
    pytest.importorskip("PySide6")
    from jsonreshape.gui import theme

    light, dark = theme.colors("light"), theme.colors("dark")
    assert light["danger_soft"].lightness() > 200 and dark["danger_soft"].lightness() < 80
    assert contrast(light["fg"], light["danger_soft"]) >= 7 and contrast(dark["fg"], dark["danger_soft"]) >= 7


@pytest.mark.gui
def test_role_style_and_bind_style(qtbot, themed) -> None:
    from PySide6.QtWidgets import QLabel

    from jsonreshape.gui import theme

    themed("light")
    light, dark = theme.colors("light"), theme.colors("dark")
    assert theme.role_style("color: $danger; background: $danger_soft;") == (
        f"color: {light['danger'].name()}; background: {light['danger_soft'].name()};")
    with pytest.raises(ValueError):
        theme.role_style("color: $no_such_role;")
    label = QLabel("x")
    qtbot.addWidget(label)
    theme.bind_style(label, "color: $warn;")
    assert label.styleSheet() == f"color: {light['warn'].name()};"
    themed("dark")
    assert label.styleSheet() == f"color: {dark['warn'].name()};"                 # followed the switch
    theme.bind_style(label, "QLabel { border: 1px solid $danger; }")            # replaces the binding
    assert label.styleSheet() == f"QLabel {{ border: 1px solid {dark['danger'].name()}; }}"
    themed("light")
    assert label.styleSheet() == f"QLabel {{ border: 1px solid {light['danger'].name()}; }}"
    assert len([o for o in label.children() if o.objectName() == "jsonreshape-theme-style"]) == 1
    theme.bind_style(label, "")                                                   # ends it
    assert label.styleSheet() == ""
    themed("dark")
    assert label.styleSheet() == ""
    # a destroyed widget takes its binding along: later theme changes do not reach it
    import shiboken6

    gone = QLabel("y")
    theme.bind_style(gone, "color: $ok;")
    shiboken6.delete(gone)
    themed("light")
    themed("dark")


# ==================================================================================================
# 3. dark-mode smoke through the real window
# ==================================================================================================
#: Messages of the offscreen test platform about what it cannot do or has not got (not problems of the program).
BENIGN_QT = ("This plugin does not support", "QFontDatabase: Cannot find font directory")
RE_SHEET_HEX = re.compile(r"#([0-9a-fA-F]{6})\b")
RE_SHEET_RGB = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")


def _records(n: int, changed: bool = False) -> list[dict]:
    cities = ["Toshkent", "Samarqand", "Farg'ona", "Тошкент", "O'zbekiston"]
    out = []
    for i in range(n):
        rec = {"id": i, "name": f"Ism {i % 7}", "city": cities[i % len(cities)], "age": 18 + (i * 7) % 60,
               "score": [0.1, 1.10, -0.0, 12345678901234567890][i % 4], "tags": ["a", "b"][: i % 3],
               "contact": {"phone": None if i % 5 == 0 else f"+998 90 {i:03d}", "ok": i % 2 == 0}}
        if changed and i % 4 == 1:
            rec["age"] += 1
            rec["city"] = "Buxoro"
        if changed and i % 9 == 2:
            del rec["tags"]
        out.append(rec)
    if changed:
        out = out[:-2] + [{"id": 1000, "name": "yangi", "city": "Xiva", "age": 30}]
    return out


@pytest.fixture
def real_window(qtbot, qapp, monkeypatch, lang_uz):
    """``start(paths) -> MainWindow`` of ``app.main(argv, run=False)``; afterwards every window is closed, no
    task is left, and the process-wide hooks of ``main()`` are undone (the e2e tests' clean-up)."""
    import gc
    import logging
    import sys
    import threading

    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    from jsonreshape import i18n
    from jsonreshape.gui import app as app_mod
    from jsonreshape.gui import theme

    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    old_font = QFont(QApplication.font())
    old_switch = sys.getswitchinterval()
    gc_was_enabled = gc.isenabled()

    class _Errors(logging.Handler):
        def __init__(self) -> None:
            super().__init__(logging.ERROR)
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            if record.name.startswith("jsonreshape"):
                self.records.append(record)

    errlog = _Errors()
    logging.getLogger().addHandler(errlog)
    started: list = []
    reported: list[BaseException] = []

    def start(paths: list[Path]):
        win = app_mod.main([str(p) for p in paths], run=False)
        qtbot.addWidget(win)
        win.app.headless = True
        win.app.tasks.set_poll_interval(20)
        win.app.error_reported.connect(reported.append)
        win.resize(1280, 860)
        started.append(win)
        return win

    try:
        yield start
        for win in started:
            if not win._closing:
                win.close()
            assert win.app.tasks.wait_idle(30)
        assert reported == [], [repr(e) for e in reported]
        assert errlog.records == [], [r.getMessage() for r in errlog.records]
    finally:
        for win in started:
            if not win._closing:
                win.close()
        logging.getLogger().removeHandler(errlog)
        sys.setswitchinterval(old_switch)
        if gc_was_enabled:
            gc.enable()
        QApplication.setFont(old_font)
        from jsonreshape.log import shutdown_logging

        shutdown_logging()
        i18n.set_language("uz")
        theme.apply_theme(qapp, "system")
        theme._reset_for_tests()


def _open_windows():
    from PySide6.QtWidgets import QApplication

    return [w for w in QApplication.topLevelWidgets() if w.isVisible()]


def _sheet_colours(top) -> dict[str, set[str]]:
    """``{widget description: colours of its style sheet}`` of ``top`` and its children (hex and rgb())."""
    from PySide6.QtWidgets import QWidget

    found: dict[str, set[str]] = {}
    for w in [top, *top.findChildren(QWidget)]:
        sheet = w.styleSheet()
        if not sheet:
            continue
        colours = {"#" + m.lower() for m in RE_SHEET_HEX.findall(sheet)}
        colours |= {"#%02x%02x%02x" % tuple(int(v) for v in m) for m in RE_SHEET_RGB.findall(sheet)}
        if colours:
            found.setdefault(f"{type(w).__name__}:{w.objectName() or '?'}", set()).update(colours)
    return found


def _scheme_only_colours(scheme: str) -> set[str]:
    """Colours of ``scheme``'s role table and palette that the other scheme does not use."""
    from jsonreshape.gui import theme

    def table(s: str) -> set[str]:
        out = {c.name() for c in theme.colors(s).values()}
        out |= {v for pair in theme._PALETTE_SPEC[s].values() for v in pair if v}
        return {c.lower() for c in out}

    other = "dark" if scheme == "light" else "light"
    return table(scheme) - table(other)


def _check_windows(qtbot, scheme: str) -> list[str]:
    """Paint every open window and list the style-sheet colours that belong to the other scheme only."""
    qtbot.wait(50)
    stale = _scheme_only_colours("dark" if scheme == "light" else "light")
    problems = []
    for top in _open_windows():
        image = top.grab()
        assert not image.isNull(), top
        for widget, colours in _sheet_colours(top).items():
            if colours & stale:
                problems.append(f"{scheme}: {type(top).__name__} '{top.windowTitle()}': {widget} keeps "
                                f"{sorted(colours & stale)} of the other scheme")
    return problems


@pytest.mark.gui
def test_the_stale_colour_check_finds_a_static_style(qtbot, themed) -> None:
    """The negative control of the smoke below: a style sheet set once with a role colour (not bound) keeps
    the old scheme's colour after a switch, and the check reports it."""
    from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

    from jsonreshape.gui import theme

    themed("dark")
    top = QWidget()
    qtbot.addWidget(top)
    static, bound = QLabel("static", top), QLabel("bound", top)
    static.setObjectName("probe_static")
    layout = QVBoxLayout(top)
    layout.addWidget(static)
    layout.addWidget(bound)
    static.setStyleSheet(f"color: {theme.color('danger').name()};")
    theme.bind_style(bound, "color: $danger;")
    top.show()
    assert _check_windows(qtbot, "dark") == []
    themed("light")
    problems = _check_windows(qtbot, "light")
    assert len(problems) == 1 and "QLabel:probe_static" in problems[0], problems
    top.close()


@pytest.mark.gui
def test_dark_mode_smoke_every_stage3_to_5_window(real_window, qtbot, qapp, qtlog, tmp_path) -> None:
    import json

    from PySide6.QtCore import QtMsgType

    from jsonreshape.gui import editors, theme

    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps(_records(40), ensure_ascii=False, indent=1), encoding="utf-8")
    b.write_text(json.dumps(_records(40, changed=True), ensure_ascii=False, indent=1), encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": "array", "items": {
        "type": "object", "required": ["id", "name"],
        "properties": {"age": {"type": "integer", "maximum": 60}, "tags": {"type": "array", "maxItems": 1}}}}),
        encoding="utf-8")
    broken = tmp_path / "broken.json"
    broken.write_text('[{"a": 1,}, {"b": NaN, \'c\': "x"}, {"d": [1, 2', encoding="utf-8")

    win = real_window([a, b])
    for tab in win.tab_widgets():
        qtbot.waitUntil(lambda tb=tab: tb.session.state in ("ready", "failed"), timeout=30000)
        assert tab.session.state == "ready"
    win.tabs.setCurrentIndex(0)
    tab = win.current_tab()
    session = tab.session
    theme.apply_theme(qapp, "dark")
    # stage 4: the structure builder
    assert tab.show_mapping() is not None
    # stage 5: statistics, duplicates, JSON Schema, repair, split / merge, compare
    panel = win.show_stats("age")
    qtbot.waitUntil(lambda: panel.status() == "done", timeout=60000)
    dupes = win.show_dupes(key="city")
    dupes.set_key("city")
    with qtbot.waitSignal(dupes.report_ready, timeout=60000):
        dupes.run_button.click()
    val = win.show_validate(schema_path=str(schema))
    val.panel.run_button.click()
    qtbot.waitUntil(lambda: not val.panel.is_running(session) and bool(val.panel.summary_text()), timeout=60000)
    assert val.panel.issue_count_shown() > 0
    win.show_generate_schema()
    rep = win.show_repair(str(broken))
    with qtbot.waitSignal(rep.run_finished, timeout=60000):
        rep.check_button.click()
    assert rep.report is not None and rep.report.total > 0
    win.show_split()
    win.show_merge()
    picker = win.show_compare_files()
    picker.accept()
    compare = picker.window_opened
    qtbot.waitUntil(lambda: compare.run_state() == "done", timeout=60000)
    compare.next_change()
    # stage 3: bulk operations and the editing dialogs (a valid and a broken JSON text, a taken key)
    win.show_bulk(session)
    good = editors.JsonEditDialog(win, '{"a": [1, 2.50, null], "b": "O\'zbekiston"}', tasks=win.app.tasks)
    bad = editors.JsonEditDialog(win, '{"a": [1, }', tasks=win.app.tasks)
    text = editors.StringDialog(win, "ikki\nqator")
    key = editors.KeyDialog(win, "name", taken={"age"})
    insert = editors.InsertDialog(win, key_needed=True, taken={"id"})
    for dlg in (good, bad, text, key, insert):
        dlg.show()
    qtbot.waitUntil(lambda: bad.status_label.text() != "" and good.status_label.text() != "", timeout=30000)
    win.show_settings()
    opened = {type(w).__name__ for w in _open_windows()}
    assert opened >= {"MainWindow", "BulkDialog", "CompareWindow", "DupesDialog", "GenerateSchemaDialog",
                      "ValidateDialog", "RepairDialog", "SplitDialog", "MergeDialog", "SettingsDialog",
                      "JsonEditDialog", "StringDialog", "KeyDialog", "InsertDialog"}, opened
    assert win.docks["stats"].isVisible() and tab.showing("mapping") is not None
    problems = _check_windows(qtbot, "dark")
    # live switches: every open window follows (nothing keeps the other scheme's colours)
    theme.apply_theme(qapp, "light")
    problems += _check_windows(qtbot, "light")
    theme.apply_theme(qapp, "dark")
    problems += _check_windows(qtbot, "dark")
    assert not problems, "\n".join(problems)
    for w in _open_windows():
        if w is not win:
            w.close()
    warnings = [r.message for r in qtlog.records
                if r.type in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg)
                and not r.message.startswith(BENIGN_QT)]
    assert not warnings, "\n".join(warnings)
