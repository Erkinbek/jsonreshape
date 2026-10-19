"""Smart values in the views (docs/ARCHITECTURE.md §14.0, §14.7, §14.9; stage-5 round B task 1).

* the grid: ``Cell.link`` underlined in the cell's (link) colour and Ctrl+click -> ``link_activated``
  only on the drawn link text, the pointing-hand cursor, ``Cell.swatch`` painted (opaque / translucent),
  in the light and the dark theme;
* the tree value column and the table cells (real sessions): tooltips of unix seconds / milliseconds
  and ISO dates in Tashkent time with the Uzbek weekday and the pre-1992 note, links (role, span between
  the quotes), colour swatches, image size tooltips; Ctrl+click asks first and opens only after "yes"
  through the injected opener (never anything real); nothing for cut / huge values except images, no
  detection in the GUI thread, no big GUI-thread reads;
* the full-value window: the smart strip (time text, swatch, link + open button, thumbnail made in the
  value task), the notes when no thumbnail is made;
* :func:`jsonreshape.gui.widgets.open_url`: only http(s) / mailto, the default opener refuses on the
  offscreen platform.
"""

from __future__ import annotations

import base64
import json
import random
import struct
import threading
from pathlib import Path
from typing import Any, NamedTuple

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QBuffer, QEvent, QIODevice, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPalette  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from jsonreshape import smartvalues  # noqa: E402
from jsonreshape.gui import theme, treemodel, widgets  # noqa: E402
from jsonreshape.gui.context import AppContext  # noqa: E402
from jsonreshape.gui.guistate import GuiState  # noqa: E402
from jsonreshape.gui.tableview import TableView  # noqa: E402
from jsonreshape.gui.treemodel import NodeSource, smart_cell, smart_value  # noqa: E402
from jsonreshape.gui.treeview import TreeView  # noqa: E402
from jsonreshape.gui.vgrid import Cell, Column, VirtualGrid  # noqa: E402
from jsonreshape.gui.widgets import FullValueDialog, smart_preview  # noqa: E402
from jsonreshape.reader import Document  # noqa: E402
from jsonreshape.settings import Settings  # noqa: E402

from views_edit_helpers import (  # noqa: E402,F401
    TIMEOUT,
    GuiReads,
    app,
    no_logged_errors,
    opened,
    views,
    write_json,
)

pytestmark = [pytest.mark.gui, pytest.mark.usefixtures("no_logged_errors")]

LEFT = Qt.MouseButton.LeftButton
CTRL = Qt.KeyboardModifier.ControlModifier
URL = "https://example.com/a?b=1"
MAIL = "mailto:ali@example.uz"


# ----------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------
def image_b64(w: int, h: int, *, fmt: str = "PNG", noise: bool = False) -> str:
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(30, 120, 200))
    if noise:
        rnd = random.Random(7)
        for y in range(h):
            for x in range(w):
                img.setPixel(x, y, rnd.getrandbits(24))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    assert img.save(buf, fmt)
    return base64.b64encode(bytes(buf.data())).decode("ascii")


def gif_b64(w: int, h: int) -> str:
    """A one-colour GIF89a (Qt reads GIF but does not write it): "uncompressed" LZW -- a clear
    code before every two pixels keeps the codes 3 bits wide."""
    clear, eoi = 4, 5
    codes: list[int] = []
    left = w * h
    while left > 0:
        codes.append(clear)
        k = min(2, left)
        codes.extend([0] * k)
        left -= k
    codes.append(eoi)
    acc = nbits = 0
    packed = bytearray()
    for c in codes:
        acc |= c << nbits
        nbits += 3
        while nbits >= 8:
            packed.append(acc & 0xFF)
            acc >>= 8
            nbits -= 8
    if nbits:
        packed.append(acc & 0xFF)
    data = bytearray([2])
    for i in range(0, len(packed), 255):
        block = packed[i:i + 255]
        data.append(len(block))
        data += block
    data.append(0)
    gif = (b"GIF89a" + struct.pack("<HHBBB", w, h, 0x80, 0, 0) + bytes((30, 120, 200, 255, 255, 255))
           + b"," + struct.pack("<HHHHB", 0, 0, w, h, 0) + bytes(data) + b";")
    return base64.b64encode(gif).decode("ascii")


@pytest.fixture
def links(monkeypatch):
    """The injected asker / opener: ``asked`` (urls), ``opened`` (urls); ``answer`` decides."""

    class Links:
        asked: list[str] = []
        opened: list[str] = []
        answer = True

    box = Links()
    box.asked, box.opened = [], []

    def asker(parent: Any, url: str, callback: Any) -> None:
        box.asked.append(url)
        callback(box.answer)

    def opener(url: str) -> bool:
        box.opened.append(url)
        return True

    monkeypatch.setattr(widgets, "url_asker", asker)
    monkeypatch.setattr(widgets, "url_opener", opener)
    return box


@pytest.fixture
def themed(qapp):
    palette = QPalette(qapp.palette())
    style = qapp.style().name()
    yield qapp
    theme._reset_for_tests()
    qapp.styleHints().unsetColorScheme()
    qapp.setStyle(style)
    qapp.setPalette(palette)
    theme.invalidate()


def find_color(image: QImage, x0: int, x1: int, y0: int, y1: int, color: QColor, tol: int = 8) -> bool:
    dpr = image.devicePixelRatio()
    for y in range(int(y0 * dpr), int(y1 * dpr) + 1):
        for x in range(int(x0 * dpr), int(x1 * dpr)):
            c = image.pixelColor(x, y)
            if abs(c.red() - color.red()) <= tol and abs(c.green() - color.green()) <= tol \
                    and abs(c.blue() - color.blue()) <= tol:
                return True
    return False


class Source:
    def __init__(self, cells: dict[tuple[int, str], Cell], rows: int) -> None:
        self.cells = cells
        self.rows = rows

    def row_count(self) -> int:
        return self.rows

    def cell(self, row: int, column: Column) -> Cell | None:
        return self.cells.get((row, column.id), Cell(text=f"r{row}"))


def make_grid(qtbot, cells: dict, rows: int = 4, mode: str = "rows") -> VirtualGrid:
    grid = VirtualGrid()
    qtbot.addWidget(grid)
    grid.resize(700, 300)
    grid.set_columns([Column("a", "A", 320), Column("b", "B", 320)])
    grid.set_selection_mode(mode)
    grid.set_source(Source(cells, rows))
    grid.show()
    qtbot.waitExposed(grid)
    QApplication.processEvents()
    return grid


def ctrl_move(grid: VirtualGrid, pos: QPoint, mods=CTRL) -> None:
    gpos = QPointF(grid.viewport().mapToGlobal(pos))
    ev = QMouseEvent(QEvent.Type.MouseMove, QPointF(pos), gpos, Qt.MouseButton.NoButton,
                     Qt.MouseButton.NoButton, mods)
    QApplication.sendEvent(grid.viewport(), ev)


def link_point(grid: VirtualGrid, row: int, col: int) -> QPoint:
    ext = grid._link_extent(row, col)
    assert ext is not None
    return QPoint((ext[0] + ext[1]) // 2, grid.cell_rect(row, col).center().y())


# ----------------------------------------------------------------------------------
# the grid: link painting / Ctrl+click / swatches, both themes
# ----------------------------------------------------------------------------------
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_grid_paints_links_and_swatches_in_both_themes(qtbot, themed, scheme) -> None:
    theme.apply_theme(themed, scheme)
    cells = {
        (0, "a"): Cell(text=URL, role="link", link=URL),
        (1, "a"): Cell(text='"' + URL + '"', role="link", link=URL, link_span=(1, len(URL) + 1)),
        (2, "a"): Cell(text="#ff8800", swatch=(255, 136, 0, 255)),
        (3, "a"): Cell(text="#00ff0080", swatch=(0, 255, 0, 128)),
    }
    grid = make_grid(qtbot, cells)
    image = grid.viewport().grab().toImage()
    link = theme.color("link")
    assert link.name() == theme.colors(scheme)["accent"].name()
    fm = grid._fms[0]
    for row in (0, 1):
        rect = grid.cell_rect(row, 0)
        x0, x1, url = grid._link_extent(row, 0)
        assert url == URL
        uy = rect.y() + grid._text_dy + max(1, fm.underlinePos())
        assert find_color(image, x0 + 1, x1 - 1, uy - 1, uy + 1, link), (scheme, row)
        # no underline left of the link (the opening quote of row 1)
        if row == 1:
            assert x0 > grid.text_rect(1, 0).x()
            assert not find_color(image, grid.text_rect(1, 0).x(), x0 - 1, uy, uy, link, tol=2)
    # an opaque swatch: its centre has the exact colour; the text moved right by the swatch
    r2 = grid.cell_rect(2, 0)
    sw = grid._swatch_w
    sx = grid.text_rect(2, 0).x() - grid._gap - sw
    sy = r2.y() + (r2.height() - sw) // 2
    dpr = image.devicePixelRatio()
    c = image.pixelColor(int((sx + sw // 2) * dpr), int((sy + sw // 2) * dpr))
    assert (c.red(), c.green(), c.blue()) == (255, 136, 0)
    assert grid.text_rect(2, 0).x() - grid.text_rect(0, 0).x() == sw + grid._gap
    # a translucent swatch blends with the checkerboard (never the pure colour, never white)
    r3 = grid.cell_rect(3, 0)
    sy3 = r3.y() + (r3.height() - sw) // 2
    c3 = image.pixelColor(int((sx + 1) * dpr), int((sy3 + 1) * dpr))
    assert c3.green() > 120 and (c3.red(), c3.blue()) != (0, 0) and c3.name() != "#ffffff"


def test_grid_ctrl_click_on_link_text_only(qtbot) -> None:
    cells = {(0, "a"): Cell(text=URL, role="link", link=URL), (1, "a"): Cell(text="plain")}
    grid = make_grid(qtbot, cells, rows=3)
    got: list = []
    grid.link_activated.connect(lambda r, c, u: got.append((r, c, u)))
    pos = link_point(grid, 0, 0)
    assert grid.link_at(pos) == (0, 0, URL)
    QTest.mouseClick(grid.viewport(), LEFT, Qt.KeyboardModifier.NoModifier, pos)
    assert got == [] and grid.current()[0] == 0          # a plain click selects as before
    grid.set_current(2, 0)
    QTest.mouseClick(grid.viewport(), LEFT, CTRL, pos)
    assert got == [(0, 0, URL)]
    assert grid.current()[0] == 2 and not grid.is_selected(0)     # the link click changed no selection
    # Ctrl+click right of the link text: no link, the selection toggles as before
    rect = grid.cell_rect(0, 0)
    beyond = QPoint(rect.right() - 3, rect.center().y())
    assert grid.link_at(beyond) is None
    QTest.mouseClick(grid.viewport(), LEFT, CTRL, beyond)
    assert len(got) == 1 and grid.is_selected(0)
    # Ctrl+double-click on the link: no "activated"
    acts: list = []
    grid.activated.connect(lambda r, c: acts.append(r))
    QTest.mouseDClick(grid.viewport(), LEFT, CTRL, pos)
    assert acts == []
    # a row without a link
    assert grid.link_at(grid.cell_rect(1, 0).center()) is None


def test_grid_pointing_hand_only_with_ctrl_over_a_link(qtbot) -> None:
    grid = make_grid(qtbot, {(0, "a"): Cell(text=URL, role="link", link=URL)}, rows=2)
    pos = link_point(grid, 0, 0)
    vp = grid.viewport()
    ctrl_move(grid, pos, Qt.KeyboardModifier.NoModifier)
    assert vp.cursor().shape() != Qt.CursorShape.PointingHandCursor
    ctrl_move(grid, pos)
    assert vp.cursor().shape() == Qt.CursorShape.PointingHandCursor
    ctrl_move(grid, grid.cell_rect(1, 0).center())
    assert vp.cursor().shape() != Qt.CursorShape.PointingHandCursor


def test_text_rect_is_public_and_follows_the_tree_layout(qtbot) -> None:
    cells = {(0, "a"): Cell(text="k", badge="string", depth=2), (1, "a"): Cell(text="k", badge="string", depth=2,
                                                                              swatch=(1, 2, 3, 255))}

    class Partial(Source):
        def cell(self, row: int, column: Column) -> Cell | None:
            if row == 2:
                return None
            return super().cell(row, column)

    grid = VirtualGrid()
    qtbot.addWidget(grid)
    grid.resize(600, 200)
    grid.set_columns([Column("a", "A", 300), Column("b", "B", 200)])
    grid.set_tree_column(0)
    grid.set_source(Partial(cells, 3))
    grid.show()
    qtbot.waitExposed(grid)
    rect = grid.cell_rect(0, 0)
    tr = grid.text_rect(0, 0)
    assert tr.x() == rect.x() + grid._pad_x + 2 * grid.indent() + grid.indent() + grid._badge_w + grid._gap
    assert tr.right() == rect.x() + rect.width() - grid._pad_x - 1 and tr.height() == rect.height()
    assert grid.text_rect(1, 0).x() == tr.x() + grid._swatch_w + grid._gap
    assert grid.text_rect(2, 0).isNull()                 # not loaded
    assert grid.text_rect(9, 0).isNull() and grid.text_rect(0, 7).isNull()
    assert grid.text_rect(0, 1).x() == grid.cell_rect(0, 1).x() + grid._pad_x   # not the tree column
    grid.set_column_visible(1, False)
    assert grid.text_rect(0, 1).isNull()


# ----------------------------------------------------------------------------------
# pure helpers
# ----------------------------------------------------------------------------------
def test_smart_value_prefix_cache_and_cell(lang_uz) -> None:
    sv = smart_value(URL)
    assert sv.kind == "url" and sv.url == URL
    assert smart_value(URL, prefix=True) is None                     # a cut link is not that link
    png = image_b64(40, 30, noise=True)
    head = smart_value(png[:300], prefix=True)                       # an image is known from its start
    assert head.kind == "image" and (head.image.width, head.image.height) == (40, 30)
    assert smart_value("1700000000", "number").kind == "unix_s"
    assert smart_value(True) is None and smart_value(None) is None and smart_value("") is None
    assert smart_value("salom") is None
    # cached per text (and language): the second call does not detect again
    calls: list = []
    real = smartvalues.detect

    def counting(*a: Any, **k: Any) -> Any:
        calls.append(a)
        return real(*a, **k)

    treemodel._smart_cache.clear()
    try:
        smartvalues.detect = counting       # (treemodel looks it up through the module)
        for _ in range(3):
            assert smart_value("#abc").kind == "color"
        assert len(calls) == 1
    finally:
        smartvalues.detect = real
    d = smart_cell(sv, '"' + URL + '"', quoted=True)
    assert d == {"tooltip": sv.tooltip, "role": "link", "link": URL, "link_span": (1, len(URL) + 1)}
    c = smart_cell(smart_value("#ff000080"), "#ff000080")
    assert c["swatch"] == (255, 0, 0, 128) and "role" not in c
    assert smart_cell(None, "x") == {}
    assert treemodel.string_value('"abc"') == ("abc", True)
    assert treemodel.string_value('"ab\\u0442…') == ("abт", False)


def test_open_url_only_http_and_mailto_after_yes(qapp, links, lang_uz) -> None:
    assert widgets.open_url(None, URL)
    assert links.asked == [URL] and links.opened == [URL]
    links.answer = False
    assert widgets.open_url(None, MAIL)
    assert links.asked == [URL, MAIL] and links.opened == [URL]           # "no": nothing opened
    for bad in ("javascript:alert(1)", "file:///C:/Windows/notepad.exe", "ftp://x.uz/f", "https://",
                "https://a b.uz", "data:text/html,<b>x</b>", ""):
        assert not widgets.open_url(None, bad), bad
    assert links.asked == [URL, MAIL]                                     # refused at once: not asked
    assert widgets.openable_url("HTTPS://Example.COM/x") and widgets.openable_url("mailto:a@b.uz")
    assert not widgets.openable_url("mailto:nobody")


def test_default_opener_never_opens_on_the_offscreen_platform(qapp, monkeypatch, lang_uz) -> None:
    opened: list = []

    class Services:
        @staticmethod
        def openUrl(url: Any) -> bool:  # noqa: N802 (Qt name)
            opened.append(url)
            return True

    monkeypatch.setattr(widgets, "QDesktopServices", Services)
    monkeypatch.setattr(widgets, "url_asker", lambda parent, url, cb: cb(True))
    monkeypatch.setattr(widgets, "url_opener", None)
    result: list = []
    assert widgets.open_url(None, URL, on_done=result.append)
    assert result == [False] and opened == []
    # the default question (confirm_handler answers it); still nothing opened offscreen
    monkeypatch.setattr(widgets, "url_asker", None)
    texts: list = []
    monkeypatch.setattr(widgets, "confirm_handler", lambda parent, text, title: texts.append((title, text)) or True)
    widgets.open_url(None, URL, on_done=result.append)
    QApplication.processEvents()
    assert result == [False, False] and opened == []
    assert texts[0][0] == "Havolani ochish" and texts[0][1].startswith("Brauzerda ochilsinmi?") and URL in texts[0][1]


# ----------------------------------------------------------------------------------
# the tree's value column
# ----------------------------------------------------------------------------------
def record(png: str, big_png: str) -> dict:
    return {"t": 1700000000, "ms": 1700000000123, "d": "2024-01-01", "dt": "1985-03-10T12:00:00+03:00",
            "url": URL, "mail": MAIL, "c": "#ff8800", "ca": "#00ff0080", "img": png, "bigimg": big_png,
            "long_url": "https://example.com/" + "a" * 400, "plain": "salom", "id": 12345678901234567890,
            "naive": "2024-02-29 23:59"}


def row_of_key(qtbot, view: TreeView, key: str) -> int:
    def find() -> int | None:
        for r in range(view.row_count()):
            text = view.cell_text(r, "key")
            if text == key:
                return r
        return None

    qtbot.waitUntil(lambda: find() is not None, timeout=TIMEOUT)
    row = find()
    qtbot.waitUntil(lambda: view.cell_at(row, "value") is not None, timeout=TIMEOUT)
    return row


def test_tree_value_column_smart_values(qtbot, opened, views, tmp_path: Path, links, monkeypatch,
                                        lang_uz) -> None:
    probe = GuiReads(monkeypatch)
    main = threading.main_thread()
    threads: list = []
    real = smartvalues.detect

    def watch(*a: Any, **k: Any) -> Any:
        threads.append(threading.current_thread() is main)
        return real(*a, **k)

    monkeypatch.setattr(smartvalues, "detect", watch)
    treemodel._smart_cache.clear()
    png = image_b64(6, 4)
    big = image_b64(64, 48, noise=True)
    assert len(big) > 600
    s = opened(write_json(tmp_path / "smart.json", [record(png, big)]))
    view = views(TreeView, s)
    qtbot.waitUntil(lambda: view.row_count() == 1, timeout=TIMEOUT)
    qtbot.waitUntil(lambda: view.cell_at(0, "key") is not None, timeout=TIMEOUT)
    view.expand_node((0,))
    qtbot.waitUntil(lambda: view.row_count() > 10, timeout=TIMEOUT)

    t_cell = view.cell_at(row_of_key(qtbot, view, "t"), "value")
    assert t_cell.text == "1700000000" and t_cell.role == "number"
    assert "Unix vaqti (soniyalarda)" in t_cell.tooltip
    assert "Toshkent vaqti: 2023-11-15 03:13:20, chorshanba (UTC+05:00)" in t_cell.tooltip
    ms = view.cell_at(row_of_key(qtbot, view, "ms"), "value")
    assert "millisoniyalarda" in ms.tooltip and "03:13:20.123" in ms.tooltip
    d = view.cell_at(row_of_key(qtbot, view, "d"), "value")
    assert d.tooltip.endswith("2024-01-01, dushanba") and d.link is None
    dt = view.cell_at(row_of_key(qtbot, view, "dt"), "value")
    assert "Toshkent vaqti: 1985-03-10 14:00:00, yakshanba" in dt.tooltip
    assert "1992 yilgacha" in dt.tooltip                                  # the pre-1992 note
    naive = view.cell_at(row_of_key(qtbot, view, "naive"), "value")
    assert "vaqt mintaqasi ko'rsatilmagan" in naive.tooltip and "payshanba" in naive.tooltip

    url_row = row_of_key(qtbot, view, "url")
    u = view.cell_at(url_row, "value")
    assert u.text == '"' + URL + '"' and u.role == "link" and u.link == URL
    assert u.link_span == (1, len(URL) + 1) and URL in u.tooltip
    m = view.cell_at(row_of_key(qtbot, view, "mail"), "value")
    assert m.link == MAIL and m.role == "link" and "ali@example.uz" in m.tooltip
    c = view.cell_at(row_of_key(qtbot, view, "c"), "value")
    assert c.swatch == (255, 136, 0, 255) and c.role == "string" and "#ff8800" in c.tooltip
    ca = view.cell_at(row_of_key(qtbot, view, "ca"), "value")
    assert ca.swatch == (0, 255, 0, 128) and "50 %" in ca.tooltip
    img = view.cell_at(row_of_key(qtbot, view, "img"), "value")
    assert img.tooltip.startswith("Rasm: PNG, 6×4 piksel") and img.link is None
    # a long image: the value column read only its beginning -- still known as an image (prefix)
    bigimg = view.cell_at(row_of_key(qtbot, view, "bigimg"), "value")
    assert bigimg.text.endswith("…") and bigimg.tooltip.startswith("Rasm: PNG, 64×48 piksel")
    # a cut URL is no link; plain strings and the huge integer have nothing
    lu = view.cell_at(row_of_key(qtbot, view, "long_url"), "value")
    assert lu.text.endswith("…") and lu.link is None and lu.tooltip is None and lu.role == "string"
    assert view.cell_at(row_of_key(qtbot, view, "plain"), "value").tooltip is None
    assert view.cell_at(row_of_key(qtbot, view, "id"), "value").tooltip is None
    # the key column is never smart
    assert view.cell_at(url_row, "key").link is None

    # Ctrl+click on the link: asked first, opened after yes (through the injected opener only)
    grid = view.grid
    col = next(i for i, cc in enumerate(grid.columns()) if cc.id == "value")
    grid.scroll_to(url_row)
    QApplication.processEvents()
    pos = link_point(grid, url_row, col)
    QTest.mouseClick(grid.viewport(), LEFT, CTRL, pos)
    assert links.asked == [URL] and links.opened == [URL]
    links.answer = False
    QTest.mouseClick(grid.viewport(), LEFT, CTRL, pos)
    assert links.asked == [URL, URL] and links.opened == [URL]
    # detection ran in the loader worker only; no big reads in the GUI thread
    assert threads and not any(threads)
    assert probe.big() == []


def test_tree_node_source_detects_in_make_info(tmp_path: Path, lang_uz) -> None:
    path = tmp_path / "s.json"
    path.write_text(json.dumps([{"u": URL, "n": 1.5, "x": "#abc", "e": "", "b": True}]), encoding="utf-8")
    doc = Document.open(path, format="array", settings=Settings(), use_cache=False)
    try:
        doc.build_index()
        src = NodeSource(doc.pinned())
        got = src.fetch([(0, i) for i in range(5)], None)
        assert got[(0, 0)].smart.kind == "url"
        assert got[(0, 1)].smart is None and got[(0, 3)].smart is None and got[(0, 4)].smart is None
        assert got[(0, 2)].smart.rgba == (170, 187, 204, 255)
        info = src.fetch([(0,)], None)[(0,)]
        assert info.smart is None              # containers have none
    finally:
        doc.close()


# ----------------------------------------------------------------------------------
# the table cells
# ----------------------------------------------------------------------------------
def table_cell(qtbot, view: TableView, row: int, path: str) -> Cell:
    qtbot.waitUntil(lambda: view.cell_at(row, path) is not None and view.cell_at(row, path).text != "",
                    timeout=TIMEOUT)
    return view.cell_at(row, path)


def test_table_cells_smart_values_and_ctrl_click(qtbot, opened, views, tmp_path: Path, links, lang_uz) -> None:
    png = image_b64(9, 5, fmt="JPEG")
    recs = [{"t": 1700000000, "u": URL, "c": "#0a0", "img": png, "list": [URL, URL], "n": 7},
            {"t": "1700000000", "u": "http://x.uz", "c": "#00000000", "img": "plain", "list": [], "n": 5e12}]
    s = opened(write_json(tmp_path / "t.json", recs))
    view = views(TableView, s)
    view.set_columns(["t", "u", "c", "img", "list[*]", "n"])
    t0 = table_cell(qtbot, view, 0, "t")
    assert t0.role == "number" and "chorshanba" in t0.tooltip
    t1 = table_cell(qtbot, view, 1, "t")
    assert t1.tooltip is None                 # a numeric STRING is not a time
    u0 = table_cell(qtbot, view, 0, "u")
    assert u0.text == URL and u0.link == URL and u0.role == "link" and u0.link_span is None
    assert table_cell(qtbot, view, 1, "u").link == "http://x.uz"
    c0 = table_cell(qtbot, view, 0, "c")
    assert c0.swatch == (0, 170, 0, 255)
    assert table_cell(qtbot, view, 1, "c").swatch == (0, 0, 0, 0)
    img = table_cell(qtbot, view, 0, "img")
    assert img.tooltip.startswith("Rasm: JPEG")
    assert table_cell(qtbot, view, 1, "img").tooltip is None
    lst = table_cell(qtbot, view, 0, "list[*]")
    assert lst.link is None and lst.text == f"{URL}; {URL}"   # several values: no smart value
    assert table_cell(qtbot, view, 1, "n").tooltip is None     # 5e12 ms is past the range (2096)

    grid = view.grid
    col = next(i for i, cc in enumerate(grid.columns()) if cc.id == "u")
    QTest.mouseClick(grid.viewport(), LEFT, CTRL, link_point(grid, 0, col))
    assert links.asked == [URL] and links.opened == [URL]
    # a plain click selects the cell, nothing is asked
    QTest.mouseClick(grid.viewport(), LEFT, Qt.KeyboardModifier.NoModifier, link_point(grid, 1, col))
    assert links.asked == [URL] and grid.current() == (1, col)


def test_table_huge_record_cut_strings_only_images(qtbot, app, opened, views, tmp_path: Path, lang_uz) -> None:
    app.settings.large_node_bytes = 4000          # records above this are streamed (strings cut at 64 K)
    long_img = image_b64(200, 130, noise=True)    # a base64 image longer than the streamed cut
    assert len(long_img) > 70_000
    recs = [{"img": long_img, "u": "https://example.com/" + "b" * 70_000, "t": "2024-01-01", "pad": "x" * 5000},
            {"img": image_b64(4, 4), "u": URL, "t": "2024-01-01"}]
    s = opened(write_json(tmp_path / "huge.json", recs))
    view = views(TableView, s)
    view.set_columns(["img", "u", "t"])
    img0 = table_cell(qtbot, view, 0, "img")
    assert img0.tooltip.startswith("Rasm: PNG, 200×130 piksel")
    assert table_cell(qtbot, view, 1, "img").tooltip.startswith("Rasm: PNG, 4×4 piksel")
    u0 = table_cell(qtbot, view, 0, "u")
    assert u0.link is None and u0.role is None                 # a cut / too long URL: no link
    assert "dushanba" in table_cell(qtbot, view, 0, "t").tooltip
    assert table_cell(qtbot, view, 1, "u").link == URL


# ----------------------------------------------------------------------------------
# the full-value window
# ----------------------------------------------------------------------------------
class Ref(NamedTuple):
    record: int | None
    path: tuple = ()
    ipath: tuple | None = None


class FakeSession:
    def __init__(self, app: AppContext, doc: Document) -> None:
        self.app = app
        self.doc = doc

    def absolute_path(self, ref: Ref) -> tuple:
        return ((ref.record,) if ref.record is not None else ()) + tuple(ref.path)

    def node(self, ref: Ref, cancel: Any = None) -> Any:
        return self.doc.node(self.absolute_path(ref), cancel=cancel)


@pytest.fixture
def fv_app(qapp, tmp_path):
    ctx = AppContext(state=GuiState(tmp_path / "gui_state.json"), headless=True)
    yield ctx
    ctx.tasks.shutdown(5)


@pytest.fixture
def fv_session(fv_app, tmp_path):
    docs: list = []

    def make(value: Any) -> FakeSession:
        path = tmp_path / f"fv{len(docs)}.json"
        path.write_text(json.dumps([value], ensure_ascii=False), encoding="utf-8")
        doc = Document.open(path, format="array", use_cache=False)
        doc.build_index()
        docs.append(doc)
        return FakeSession(fv_app, doc)

    yield make
    for d in docs:
        d.close()


def open_full(qtbot, session: FakeSession, ref: Ref, **kw: Any) -> FullValueDialog:
    dlg = FullValueDialog(session, ref, **kw)
    qtbot.addWidget(dlg)
    with qtbot.waitSignal(dlg.loaded, timeout=TIMEOUT):
        dlg.show()
    return dlg


def test_full_value_window_time_colour_link(qtbot, fv_session, links, lang_uz) -> None:
    s = fv_session({"dt": "2024-01-01T10:00:00Z", "c": "#ff000080", "u": URL, "ms": 1700000000123, "p": "x"})
    dlg = open_full(qtbot, s, Ref(0, ("dt",)))
    assert dlg.smart_frame.isVisibleTo(dlg) and dlg.smart_value().kind == "datetime"
    assert "Toshkent vaqti: 2024-01-01 15:00:00, dushanba (UTC+05:00)" in dlg.smart_label.text()
    assert not dlg.swatch_label.isVisibleTo(dlg) and not dlg.link_label.isVisibleTo(dlg)
    dlg.close()

    dlg = open_full(qtbot, s, Ref(0, ("c",)))
    assert dlg.swatch_label.isVisibleTo(dlg) and "ko'rinish darajasi 50 %" in dlg.smart_label.text()
    pm = dlg.swatch_label.pixmap().toImage()
    mid = pm.pixelColor(pm.width() // 2, pm.height() // 2)
    assert mid.red() > 200 and mid.green() < 130 and mid.blue() < 130      # red over the checkerboard
    dlg.close()

    dlg = open_full(qtbot, s, Ref(0, ("u",)))
    assert dlg.link_label.isVisibleTo(dlg) and dlg.link_label.text() == URL
    assert dlg.open_button.isVisibleTo(dlg) and dlg.open_button.text() == "Brauzerda ochish…"
    assert dlg.link_label.font().underline()
    assert dlg.link_label.palette().color(QPalette.ColorRole.WindowText).name() == theme.color("link").name()
    links.answer = False
    dlg.open_button.click()
    assert links.asked == [URL] and links.opened == []
    links.answer = True
    QTest.mouseClick(dlg.link_label, LEFT)
    assert links.asked == [URL, URL] and links.opened == [URL]
    dlg.close()

    dlg = open_full(qtbot, s, Ref(0, ("ms",)))
    assert dlg.smart_value().kind == "unix_ms" and "03:13:20.123" in dlg.smart_label.text()
    dlg.close()
    dlg = open_full(qtbot, s, Ref(0, ("p",)))
    assert dlg.smart_value() is None and not dlg.smart_frame.isVisibleTo(dlg)
    dlg.close()
    dlg = open_full(qtbot, s, Ref(0, ()))         # a container: nothing
    assert dlg.smart_value() is None
    dlg.close()


def test_full_value_window_thumbnail(qtbot, fv_session, monkeypatch, lang_uz) -> None:
    main = threading.main_thread()
    where: list = []
    real = widgets._decode_thumbnail

    def watch(*a: Any, **k: Any) -> Any:
        where.append(threading.current_thread() is main)
        return real(*a, **k)

    monkeypatch.setattr(widgets, "_decode_thumbnail", watch)
    big = image_b64(400, 200, noise=False)
    data_url = "data:image/gif;base64," + gif_b64(30, 60)
    s = fv_session({"png": big, "gif": data_url, "webp": image_b64(20, 10, fmt="WEBP"), "bad": "iVBORw0KGgoAAAAA",
                    "jpg": "data:image/jpeg;base64," + image_b64(50, 20, fmt="JPEG")})
    dlg = open_full(qtbot, s, Ref(0, ("png",)))
    assert dlg.thumb_label.isVisibleTo(dlg)
    pm = dlg.thumb_label.pixmap()
    assert (pm.width(), pm.height()) == (widgets.THUMB_SIZE, widgets.THUMB_SIZE // 2)
    assert dlg.smart_label.text() == "Rasm: PNG, 400×200 piksel"        # no hint pointing to this window
    assert dlg.result.thumbnail is not None and where == [False]        # decoded in the value task
    dlg.close()
    dlg = open_full(qtbot, s, Ref(0, ("gif",)))
    pm = dlg.thumb_label.pixmap()
    assert (pm.width(), pm.height()) == (30, 60)                         # small images keep their size
    dlg.close()
    dlg = open_full(qtbot, s, Ref(0, ("webp",)))
    assert dlg.smart_value().image.format == "WEBP" and dlg.thumb_label.isVisibleTo(dlg)
    dlg.close()
    dlg = open_full(qtbot, s, Ref(0, ("jpg",)))
    assert dlg.smart_value().image.format == "JPEG" and dlg.smart_value().image.data_url
    assert (dlg.thumb_label.pixmap().width(), dlg.thumb_label.pixmap().height()) == (50, 20)
    dlg.close()
    dlg = open_full(qtbot, s, Ref(0, ("bad",)))       # PNG magic, no image behind it
    assert dlg.result.thumbnail is None
    dlg.close()
    # a value cut at the window's limit: no thumbnail, a note
    dlg = open_full(qtbot, s, Ref(0, ("png",)), cap=200)
    assert dlg.result.truncated and dlg.result.thumbnail is None
    assert "to'liq yuklanmagan" in dlg.smart_label.text()
    dlg.close()
    # too many pixels: no decode at all, a note
    monkeypatch.setattr(widgets, "THUMB_MAX_PIXELS", 1000)
    where.clear()
    dlg = open_full(qtbot, s, Ref(0, ("png",)))
    assert dlg.result.thumbnail is None and where == [] and "juda katta (400×200" in dlg.smart_label.text()
    dlg.close()


def test_full_value_link_follows_the_theme(qtbot, fv_session, themed, lang_uz) -> None:
    theme.apply_theme(themed, "light")
    s = fv_session({"u": URL})
    dlg = open_full(qtbot, s, Ref(0, ("u",)))
    color = dlg.link_label.palette().color(QPalette.ColorRole.WindowText).name()
    assert color == theme.colors("light")["link"].name()
    theme.apply_theme(themed, "dark")
    color = dlg.link_label.palette().color(QPalette.ColorRole.WindowText).name()
    assert color == theme.colors("dark")["link"].name() != theme.colors("light")["link"].name()
    dlg.close()


def test_smart_preview_directly(qapp, lang_en) -> None:
    full = widgets.FullText("https://x.uz/a", "string", False, 10)
    assert smart_preview(full).smart.kind == "url"
    full = widgets.FullText("https://x.uz/a", "string", True, 10)
    assert smart_preview(full).smart is None                     # cut: only images count
    full = widgets.FullText("1700000000\n", "number", False, 10)
    assert smart_preview(full).smart.kind == "unix_s"
    full = widgets.FullText("{}", "object", False, 2)
    assert smart_preview(full).smart is None
