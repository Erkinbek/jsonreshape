"""jsonreshape.gui.theme: color roles (light/dark, the prototype palette), badges, fonts,
apply_theme() (Fusion + palette, theme_changed, system scheme following)."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from jsonreshape.gui import theme  # noqa: E402

pytestmark = pytest.mark.gui

ROOT = Path(__file__).resolve().parents[1]


def _luminance(c: QColor) -> float:
    def ch(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * ch(c.redF()) + 0.7152 * ch(c.greenF()) + 0.0722 * ch(c.blueF())


def contrast(a: QColor, b: QColor) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@pytest.fixture
def restore_theme(qapp):
    """apply_theme changes the global application state: restore it afterwards."""
    palette = QPalette(qapp.palette())
    style = qapp.style().name()
    yield qapp
    theme._reset_for_tests()
    try:
        qapp.styleHints().unsetColorScheme()
    except AttributeError:  # pragma: no cover
        pass
    qapp.setStyle(style)
    qapp.setPalette(palette)
    theme.invalidate()


# ----------------------------------------------------------------------------- pure data
@pytest.mark.parametrize("scheme", theme.SCHEMES)
def test_colors_cover_every_role(scheme) -> None:
    table = theme.colors(scheme)
    required = {"key", "string", "number", "bool", "null", "object", "array", "muted", "error", "link",
                "match", "current_match", "guide", "badge_text", "warn", "danger", "ok", "accent",
                "accent_soft", "line", "bg", "panel", "fg", "faint", "selection", "boolean"}
    assert required <= set(table)
    assert set(theme.ROLES) == set(table)
    assert all(isinstance(c, QColor) and c.isValid() for c in table.values())
    # aliases
    assert table["error"] == table["danger"] and table["link"] == table["accent"]
    assert table["boolean"] == table["bool"]
    # fresh objects: mutating one result never changes the table
    table["fg"].setRed(1)
    assert theme.colors(scheme)["fg"] != table["fg"]


def test_prototype_palette_values() -> None:
    light, dark = theme.colors("light"), theme.colors("dark")
    expected_light = {"bg": "#f2f4f7", "panel": "#ffffff", "fg": "#17202c", "accent": "#2a57c4",
                      "object": "#4a53b5", "array": "#a0600f", "string": "#2b7649", "number": "#1e68a3",
                      "bool": "#8344ad", "null": "#77808f", "danger": "#b23434", "warn": "#9a5a08"}
    expected_dark = {"bg": "#0e1117", "panel": "#151a22", "fg": "#e3e7ef", "accent": "#7e9cff",
                     "object": "#a0a8ff", "array": "#e2a45a", "string": "#72cf98", "number": "#6db5ef",
                     "bool": "#c79cf0", "null": "#8b94a5", "danger": "#f07a7a", "ok": "#72cf98"}
    assert {k: light[k].name() for k in expected_light} == expected_light
    assert {k: dark[k].name() for k in expected_dark} == expected_dark


@pytest.mark.parametrize("scheme", theme.SCHEMES)
def test_colors_are_readable(scheme) -> None:
    c = theme.colors(scheme)
    panel = c["panel"]
    assert contrast(c["fg"], panel) >= 7
    assert contrast(c["key"], panel) >= 4.5
    for role in ("string", "number", "bool", "object", "array", "muted", "danger", "accent"):
        assert contrast(c[role], panel) >= 4.0, role
    assert contrast(c["null"], panel) >= 3.0
    # search highlights keep the text readable
    for role in ("fg", "string", "number", "key"):
        assert contrast(c[role], c["match"]) >= 3.0, role
        assert contrast(c[role], c["current_match"]) >= 2.5, role
    # the type colors and the key color are pairwise distinct
    kinds = [c[k].name() for k in ("key", "string", "number", "bool", "null", "object", "array")]
    assert len(set(kinds)) == len(kinds)
    # selection is visible but subtle; dark really is dark
    assert c["selection"] != panel and contrast(c["fg"], c["selection"]) >= 7
    assert (panel.lightness() < 100) == (scheme == "dark")


def test_unknown_scheme_rejected() -> None:
    with pytest.raises(ValueError):
        theme.colors("sepia")
    with pytest.raises(ValueError):
        theme.build_palette("sepia")


def test_badges() -> None:
    for kind in theme.TYPES:
        glyph = theme.BADGES[kind]
        assert glyph and len(glyph) <= 4
    assert theme.BADGES["boolean"] == theme.BADGES["bool"]
    assert len(set(theme.BADGES[k] for k in theme.TYPES)) == len(theme.TYPES)
    assert theme.badge_glyph("nope") == "?"


def test_theme_data_needs_no_application_and_no_other_gui_module(tmp_path) -> None:
    code = (
        "import sys\n"
        "from jsonreshape.gui import theme\n"
        "c = theme.colors('dark')\n"
        "assert c['string'].name() == '#72cf98'\n"
        "assert theme.color('number').name() == '#1e68a3'\n"   # no app: light
        "assert theme.current_scheme() == 'light'\n"
        "others = sorted(m for m in sys.modules if m.startswith('jsonreshape.gui.') and m != 'jsonreshape.gui.theme')\n"
        "assert not others, others\n"
        "assert 'PySide6.QtWidgets' not in sys.modules\n"
        "print('ok')\n"
    )
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def test_theme_source_imports_no_gui_sibling() -> None:
    tree = ast.parse((ROOT / "jsonreshape" / "gui" / "theme.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("jsonreshape.gui"), node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("jsonreshape.gui"), alias.name


# ----------------------------------------------------------------------------- application
def test_apply_theme_switches_palette_and_emits(qtbot, restore_theme) -> None:
    app = restore_theme
    with qtbot.waitSignal(theme.notifier().theme_changed, timeout=1000):
        theme.apply_theme(app, "dark")
    assert app.style().name().lower() == "fusion"
    assert theme.mode() == "dark" and theme.current_scheme() == "dark" and theme.is_dark()
    pal = app.palette()
    assert pal.color(QPalette.ColorRole.Window).name() == "#0e1117"
    assert pal.color(QPalette.ColorRole.Base).name() == "#151a22"
    assert pal.color(QPalette.ColorRole.Text).name() == "#e3e7ef"
    assert pal.color(QPalette.ColorRole.Highlight).name() == "#7e9cff"
    assert pal.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text).name() == "#6d7889"
    assert theme.color("string").name() == "#72cf98"

    with qtbot.waitSignal(theme.notifier().theme_changed, timeout=1000):
        theme.apply_theme(app, "light")
    assert app.palette().color(QPalette.ColorRole.Window).name() == "#f2f4f7"
    assert theme.color("string").name() == "#2b7649"          # cache invalidated
    assert theme.color("error") == theme.color("danger")
    assert theme.color("no-such-role") == theme.color("fg")
    assert theme.badge_color("number") == theme.color("number")
    assert theme.badge_color("boolean") == theme.color("bool")
    assert theme.badge_color("weird") == theme.color("muted")
    tint = theme.badge_background("string")
    assert 0 < tint.alpha() < 128 and tint.rgb() == theme.color("string").rgb()

    # an unknown mode falls back to "system"
    theme.apply_theme(app, "neon")
    assert theme.mode() == "system"


def test_system_mode_follows_the_scheme(qtbot, restore_theme, monkeypatch) -> None:
    app = restore_theme
    theme.apply_theme(app, "system")
    scheme = {"value": "dark"}
    monkeypatch.setattr(theme, "system_scheme", lambda: scheme["value"])
    with qtbot.waitSignal(theme.notifier().theme_changed, timeout=1000):
        theme._on_system_scheme_changed()
    assert app.palette().color(QPalette.ColorRole.Base).name() == "#151a22"
    assert theme.current_scheme() == "dark"
    # no change -> no signal
    with qtbot.assertNotEmitted(theme.notifier().theme_changed):
        theme._on_system_scheme_changed()
    # a forced mode ignores the system
    theme.apply_theme(app, "light")
    scheme["value"] = "dark"
    with qtbot.assertNotEmitted(theme.notifier().theme_changed):
        theme._on_system_scheme_changed()
    assert app.palette().color(QPalette.ColorRole.Base).name() == "#ffffff"


def test_system_scheme_falls_back_to_palette_lightness(restore_theme, monkeypatch) -> None:
    from PySide6.QtCore import Qt

    class Hints:
        def colorScheme(self):  # noqa: N802
            return Qt.ColorScheme.Unknown

    monkeypatch.setattr(theme.QGuiApplication, "styleHints", staticmethod(lambda: Hints()))
    monkeypatch.setattr(theme, "_system_palette", theme.build_palette("dark"))
    assert theme.system_scheme() == "dark"
    monkeypatch.setattr(theme, "_system_palette", theme.build_palette("light"))
    assert theme.system_scheme() == "light"


@pytest.mark.parametrize("scheme", theme.SCHEMES)
def test_build_palette_is_complete(scheme, qapp) -> None:
    pal = theme.build_palette(scheme)
    cr = QPalette.ColorRole
    window, text = pal.color(cr.Window), pal.color(cr.WindowText)
    assert (window.lightness() < text.lightness()) == (scheme == "dark")
    for role in (cr.Base, cr.AlternateBase, cr.Button, cr.ButtonText, cr.ToolTipBase, cr.ToolTipText,
                 cr.Highlight, cr.HighlightedText, cr.PlaceholderText, cr.Link, cr.Light, cr.Dark, cr.Mid):
        assert pal.color(role).isValid()
    assert contrast(pal.color(cr.Highlight), pal.color(cr.HighlightedText)) >= 4.5
    assert contrast(pal.color(cr.ButtonText), pal.color(cr.Button)) >= 7


def test_fonts(qapp) -> None:
    font = theme.mono_font()
    assert font.fixedPitch()            # requested; the offscreen test platform may lack real fonts
    assert font.styleHint() == font.StyleHint.Monospace
    family = theme.default_mono_family()
    assert font.family() == family
    assert font.pointSizeF() >= 10
    sized = theme.mono_font("Courier New", 13.5)
    assert sized.family() == "Courier New" and sized.pointSizeF() == pytest.approx(13.5)
    assert theme.mono_font(point_size=0).pointSizeF() >= 10        # invalid -> default
    ui = theme.ui_font(15)
    assert ui.pointSizeF() == pytest.approx(15)
    assert theme.ui_font().family() == QApplication.font().family()
