"""Screenshots of the jsonreshape viewer for the README (``docs/img/*.png``) -- a developer tool, not part of the
program and not collected by pytest.

Run from the project root (the venv with PySide6 and the package installed)::

    .venv\\Scripts\\python scripts\\make_screenshots.py                # every picture into docs/img/
    .venv\\Scripts\\python scripts\\make_screenshots.py --only stats palette
    .venv\\Scripts\\python scripts\\make_screenshots.py --out C:\\tmp\\img --work C:\\tmp\\shots-work

The real window is started offscreen (``QT_QPA_PLATFORM=offscreen``, English texts, a private ``JSONRESHAPE_HOME``
so the user's own GUI state and session are neither read nor written) through :func:`jsonreshape.gui.app.main`
with ``run=False`` -- the way ``tests/test_gui_e2e.py`` and ``packaging/entry_gui.py`` drive it -- on synthetic data
written by ``bench/make_synthetic.py`` into the work folder (12 000 records, about 4 MB; a second file with a few
hundred records changed, removed and added for the comparison; a small CSV for the import window). Every picture is
``QWidget.grab()`` of a whole window (nothing is cropped): the main window at 1400 x 900, the compare window at the
same size, the import window at its own size. PNGs are written with the highest zlib level and, when one would be
over :data:`MAX_BYTES`, reduced to an 8-bit palette (a median-cut quantisation written here; Pillow is not a
dependency of the project).

Pictures (``--only`` takes these names):

``main-light`` / ``main-dark``
    the tree with a record expanded (contact, address, orders) and another record selected, its text in the right
    pane; the same in the dark theme.
``structure-builder``
    the structure builder (Ctrl+6) over the whole window after flattening ``children`` (the live preview shows the
    result).
``compare``
    the compare window of the two generated files (key ``id``): results grid and the first changed record's diff.
``stats``
    the field statistics dock for ``score`` with its histogram.
``import-dialog``
    File > Import with the CSV chosen (detected type, options, target).
``palette``
    the command palette (Ctrl+Shift+P) with the query "export".

Exit code 1 when a picture could not be taken (the reason is printed) or the program reported an error.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

# -- environment before Qt or the package is imported -------------------------------------------------------------
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["JSONRESHAPE_LANG"] = "en"
os.environ.setdefault("QT_FORCE_STDERR_LOGGING", "1")
if os.environ["QT_QPA_PLATFORM"] == "offscreen" and sys.platform == "win32" and "QT_QPA_FONTDIR" not in os.environ:
    # the offscreen platform uses the FreeType font database, which loads fonts from this folder only (without it
    # every text is drawn as a box): the system fonts (Segoe UI, Consolas, Cascadia Mono)
    os.environ["QT_QPA_FONTDIR"] = os.path.join(os.environ.get("WINDIR") or r"C:\Windows", "Fonts")

UI_FONT_FAMILIES = ("Segoe UI", "Arial", "DejaVu Sans")   # the first installed one, 9 pt (the Windows UI font)
UI_FONT_POINTS = 9.0

SHOTS = ("main-light", "main-dark", "structure-builder", "compare", "stats", "import-dialog", "palette")
WINDOW_SIZE = (1400, 900)
MAX_BYTES = 350 * 1024
RECORDS = 12_000
TIMEOUT_S = 120.0
SETTLE_MS = 400         # event processing after the last change, before a grab (layouts, repaints, fonts)


def _root() -> Path:
    here = Path(__file__).resolve()
    for candidate in (here.parent, here.parent.parent):
        if (candidate / "jsonreshape" / "__init__.py").is_file() and (candidate / "bench").is_dir():
            return candidate
    return Path.cwd()


# ===================================================================================================== data files
def make_data(work: Path, root: Path) -> dict[str, Path]:
    """The synthetic files of the pictures (written once; ``--force`` regenerates)."""
    work.mkdir(parents=True, exist_ok=True)
    customers = work / "customers.json"
    if not customers.is_file():
        subprocess.run([sys.executable, str(root / "bench" / "make_synthetic.py"), "--records", str(RECORDS),
                        "--out", str(customers), "--force", "-q"], check=True)
    changed = work / "customers-2026-10.json"
    if not changed.is_file():
        _write_changed_copy(customers, changed)
    csv_path = work / "data" / "orders.csv"          # shown in the import window as the relative data\orders.csv
    if not csv_path.is_file():
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        _write_csv(csv_path)
    return {"customers": customers, "changed": changed, "csv": csv_path}


def _write_changed_copy(source: Path, target: Path) -> None:
    """``target``: ``source`` with every 40th record changed (city, score, a tag), every 97th removed and a few new
    ones appended -- one record per line, every untouched line byte for byte as in ``source``."""
    lines = source.read_text(encoding="utf-8").split("\n")
    body = [ln for ln in lines if ln.startswith("{")]
    out: list[str] = []
    for i, line in enumerate(body):
        if i % 97 == 50:
            continue
        if i % 40 == 7:
            rec = json.loads(line.rstrip(","))
            rec["contact"]["address"]["city"] = "Qarshi" if rec["contact"]["address"]["city"] != "Qarshi" else "Xiva"
            rec["score"] = round(float(rec["score"]) + 100.0, 2)
            rec["tags"] = list(rec.get("tags", [])) + ["yangilangan"]
            out.append(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
        else:
            out.append(line.rstrip(","))
    last_id = len(body)
    for k in range(5):
        rec = {"id": last_id + k, "name": ["Dilnoza Karimova", "Jasur Yusupov", "Феруза Тошматова", "Otabek Ergashev",
                                          "Madina Qodirova"][k],
               "active": True, "score": 640.5 + k, "created_at": 1_749_000_000 + k * 86400,
               "contact": {"phone": f"+99890{7000000 + k}",
                           "address": {"city": "Toshkent", "street": "Amir Temur ko'chasi", "zip": 100000 + k}},
               "tags": ["yangi"], "orders": [{"oid": (last_id + k) * 10, "sum": 120.0 + k, "items": 2}]}
        out.append(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
    tmp = target.with_suffix(".part")
    tmp.write_text("[\n" + ",\n".join(out) + "\n]\n", encoding="utf-8")
    os.replace(tmp, target)


def _write_csv(path: Path) -> None:
    cities = ["Toshkent", "Samarqand", "Buxoro", "Andijon", "Namangan", "Farg'ona", "Xiva", "Nukus", "Qarshi"]
    names = ["Abdulla Karimov", "Gulnora Rahimova", "Bobur Yusupov", "Nodira Toshmatova", "Sardor Qodirov",
             "Dilnoza Ergasheva", "Jasur Xolmatov", "Kamola Nazarova"]
    rows = ["order_id,customer,city,sum,items,paid,created"]
    for i in range(1, 61):
        rows.append(f"{1000 + i},{names[i % len(names)]},{cities[i % len(cities)]},{(i * 37) % 500}.{i % 100:02d},"
                    f"{1 + i % 7},{'true' if i % 3 else 'false'},2026-{1 + i % 12:02d}-{1 + i % 28:02d}")
    tmp = path.with_suffix(".part")
    tmp.write_text("\n".join(rows) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ==================================================================================================== Qt helpers
def process(ms: int = 0) -> None:
    """Process events for ``ms`` milliseconds (at least once)."""
    from PySide6.QtWidgets import QApplication

    end = time.monotonic() + ms / 1000.0
    while True:
        QApplication.processEvents()
        if time.monotonic() >= end:
            return
        time.sleep(0.01)


def wait_until(pred: Callable[[], Any], what: str, timeout: float = TIMEOUT_S) -> None:
    end = time.monotonic() + timeout
    while True:
        if pred():
            return
        if time.monotonic() > end:
            raise TimeoutError(f"timed out waiting for {what}")
        process(10)


def wait_ready(tab: Any) -> Any:
    wait_until(lambda: tab.session.state in ("ready", "failed"), f"{tab.session.name} to open")
    if tab.session.state != "ready":
        raise RuntimeError(f"{tab.session.name} did not open: {tab.session.open_error}")
    return tab


def expand(tree: Any, kp: tuple) -> None:
    done: list[bool] = []
    tree.expand_node(kp, then=lambda: done.append(True), otherwise=lambda: done.append(False))
    wait_until(lambda: bool(done), f"the tree node {kp} to expand")
    if not done[0]:
        raise RuntimeError(f"the tree node {kp} could not be expanded")


# ================================================================================================== PNG writing
def _pixel_counts(image: Any) -> dict[int, int]:
    """``{0xffRRGGBB: pixels}`` of an ``RGB32`` image."""
    from collections import Counter

    width = image.width()
    if image.bytesPerLine() == width * 4:
        return Counter(memoryview(image.constBits()).cast("I"))
    counts: Counter[int] = Counter()
    for y in range(image.height()):
        counts.update(memoryview(image.constScanLine(y))[: width * 4].cast("I"))
    return counts


def _median_cut_palette(image: Any, colours: int = 256) -> list[int]:
    """A palette of at most ``colours`` values (``0xffRRGGBB``) for ``image`` by median cut over its distinct
    colours (weighted by their pixel counts)."""
    counts = _pixel_counts(image)
    if len(counts) <= colours:
        return sorted(counts)
    boxes: list[list[tuple[int, int]]] = [[(c, n) for c, n in counts.items()]]
    channels = ((16, 0xFF), (8, 0xFF), (0, 0xFF))
    while len(boxes) < colours:
        best, best_span, best_ch = -1, -1, 0
        for i, box in enumerate(boxes):
            if len(box) < 2:
                continue
            for ch, (shift, mask) in enumerate(channels):
                vals = [(c >> shift) & mask for c, _ in box]
                span = max(vals) - min(vals)
                if span > best_span:
                    best, best_span, best_ch = i, span, ch
        if best < 0:
            break
        shift = channels[best_ch][0]
        box = sorted(boxes.pop(best), key=lambda cn: (cn[0] >> shift) & 0xFF)
        total = sum(n for _, n in box)
        acc, cut = 0, 0
        for cut, (_, n) in enumerate(box):
            acc += n
            if acc * 2 >= total:
                break
        cut = max(1, min(len(box) - 1, cut + 1))
        boxes.append(box[:cut])
        boxes.append(box[cut:])
    palette = []
    for box in boxes:
        total = sum(n for _, n in box)
        r = sum(((c >> 16) & 0xFF) * n for c, n in box) // total
        g = sum(((c >> 8) & 0xFF) * n for c, n in box) // total
        b = sum((c & 0xFF) * n for c, n in box) // total
        palette.append(0xFF000000 | (r << 16) | (g << 8) | b)
    return palette


def save_png(pixmap: Any, path: Path, *, max_bytes: int = MAX_BYTES) -> int:
    """Write ``pixmap`` as a PNG (zlib level 9); over ``max_bytes`` it is written again with an 8-bit palette
    (median cut, nearest colour, no dithering: flat UI colours stay exact). Returns the final size."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QImageWriter

    def write(img: Any) -> int:
        writer = QImageWriter(str(path), b"png")
        writer.setCompression(100)        # Qt's PNG writer: 0 = no compression ... 100 = the highest zlib level
        if not writer.write(img):
            raise RuntimeError(f"could not write {path}: {writer.errorString()}")
        return path.stat().st_size

    image = pixmap.toImage().convertToFormat(QImage.Format.Format_RGB32)
    size = write(image)
    if size <= max_bytes:
        return size
    palette = _median_cut_palette(image)
    flags = Qt.ImageConversionFlag.ThresholdDither | Qt.ImageConversionFlag.AvoidDither   # nearest colour, no dither
    indexed = image.convertToFormat(QImage.Format.Format_Indexed8, palette, flags)
    return write(indexed)


# ========================================================================================================= shots
class Shooter:
    """The started application and the pictures."""

    def __init__(self, files: dict[str, Path], out: Path) -> None:
        from PySide6.QtGui import QFont, QFontDatabase
        from PySide6.QtWidgets import QApplication

        from jsonreshape.gui import app as app_mod
        from jsonreshape.gui import theme

        self.files = files
        self.out = out
        self.theme = theme
        self.errors: list[BaseException] = []
        self.sizes: dict[str, int] = {}
        # the application is created here so that the UI font is the Windows one before the window is built
        # (the FreeType database's default would be a substitute for "Helvetica" at 12 pt)
        qapp = QApplication.instance() or QApplication([sys.argv[0]])
        installed = set(QFontDatabase.families())
        family = next((f for f in UI_FONT_FAMILIES if f in installed), None)
        if family is not None:
            qapp.setFont(QFont(family, UI_FONT_POINTS))
        print(f"fonts: {len(installed)} families, UI font {qapp.font().family()} {qapp.font().pointSizeF():g} pt")
        self.win = app_mod.main([str(files["customers"])], run=False)
        if not hasattr(self.win, "tab_widgets"):
            raise RuntimeError(f"the window did not start: {self.win!r}")
        self.qapp = QApplication.instance()
        self.app = self.win.app
        self.app.headless = True                   # errors are collected (printed at the end), not shown in dialogs
        self.app.tasks.set_poll_interval(20)
        self.app.error_reported.connect(self.errors.append)
        self.win.resize(*WINDOW_SIZE)
        self.theme.apply_theme(self.qapp, "light")
        self.tab = wait_ready(self.win.current_tab())
        self.session = self.tab.session
        process(200)

    # -- helpers ----------------------------------------------------------------------------------------------
    def grab(self, name: str, widget: Any = None) -> Path:
        widget = widget if widget is not None else self.win
        process(SETTLE_MS)
        if widget is self.win and (widget.width(), widget.height()) != WINDOW_SIZE:
            widget.resize(*WINDOW_SIZE)                                 # a dock may have pushed it wider
            process(SETTLE_MS)
            if (widget.width(), widget.height()) != WINDOW_SIZE:
                raise RuntimeError(f"the window is {widget.width()} x {widget.height()}, not {WINDOW_SIZE} "
                                   "(its minimum size is larger: make the dock narrower)")
        path = self.out / f"{name}.png"
        self.sizes[name] = save_png(widget.grab(), path)
        print(f"  {path.name}: {widget.width()} x {widget.height()} px, {self.sizes[name] / 1024:.0f} KB")
        return path

    def tree(self) -> Any:
        pane = self.tab.showing("tree")
        if pane is None:
            self.tab.panes[0].set_view("tree")
            pane = self.tab.panes[0]
        return pane.current_view()

    def text_view(self) -> Any:
        pane = self.tab.showing("text")
        if pane is None:
            self.tab.set_second_pane_visible(True)
            self.tab.panes[1].set_view("text")
            pane = self.tab.panes[1]
        return pane.current_view()

    def prepare_tree(self) -> None:
        """Record #0 expanded with contact > address and tags, record #1 with its orders, record #1 selected."""
        tree = self.tree()
        text = self.text_view()
        wait_until(lambda: tree.cell_text(0, "key") is not None, "the first tree row")
        keys = ("id", "name", "active", "score", "created_at", "contact", "tags", "orders", "children", "note")
        contact, tags, orders = keys.index("contact"), keys.index("tags"), keys.index("orders")
        expand(tree, (0,))
        expand(tree, (0, contact))
        expand(tree, (0, contact, 1))                                   # address
        expand(tree, (0, tags))
        expand(tree, (1,))
        expand(tree, (1, orders))
        row = tree.row_of((1,))
        if row is None:
            raise RuntimeError("record #1 has no row")
        tree.select_row(row)
        wait_until(lambda: self.session.current is not None and self.session.current.record == 1
                   and tuple(self.session.current.path) == (), "record #1 to be the current node")
        wait_until(lambda: not text.is_loading() and '"name"' in text.text(), "the text pane")
        tree.grid.setFocus()
        process(100)

    # -- the pictures ------------------------------------------------------------------------------------------
    def shot_main_light(self) -> None:
        self.theme.apply_theme(self.qapp, "light")
        self.prepare_tree()
        self.grab("main-light")

    def shot_main_dark(self) -> None:
        self.theme.apply_theme(self.qapp, "dark")
        self.prepare_tree()
        self.grab("main-dark")
        self.theme.apply_theme(self.qapp, "light")
        process(100)

    def shot_structure_builder(self) -> None:
        self.theme.apply_theme(self.qapp, "light")
        self.tab.set_second_pane_visible(False)                         # the builder over the whole window
        self.tab.set_active_pane(0)
        view = self.tab.show_mapping()
        if view is None:
            raise RuntimeError("the structure builder could not be shown")
        wait_until(lambda: view.state.designer is not None, "the structure builder's tree")
        settle = lambda: (not view._preview_timer.isActive() and view._preview_task is None   # noqa: E731
                          and not view._preview_dirty and not view.state.busy)
        wait_until(settle, "the first preview")
        children = view.state.tree.find("[].children")
        if children is None:
            raise RuntimeError("the sample has no [].children")
        view.run_action("flatten", children.id)
        wait_until(lambda: not view.state.busy, "the flatten")
        wait_until(settle, "the preview after the flatten")
        elem = view.state.tree.find("[].children[]")
        if elem is not None:
            view.select_node(elem.id)
        wait_until(lambda: not view.toast_text(), "the flatten toast to go", timeout=15.0)   # (TOAST_MS)
        self.grab("structure-builder")
        # back to tree + text for the pictures after this one
        self.tab.panes[0].set_view("tree")
        self.tab.set_second_pane_visible(True)
        self.tab.set_active_pane(0)
        process(100)

    def shot_compare(self) -> None:
        self.theme.apply_theme(self.qapp, "light")
        other = self.win.open_path(self.files["changed"])
        wait_ready(other)
        self.win.tabs.setCurrentWidget(self.tab)
        process(50)
        picker = self.win.show_compare_files()
        if picker is None:
            raise RuntimeError("the compare dialog did not open")
        wait_until(lambda: "id" in picker.key_fields(), "the key list of the compare dialog")
        combo_b = picker.side_widgets["b"][0]
        if combo_b.currentData() is not other.session:
            index = next((i for i in range(combo_b.count()) if combo_b.itemData(i) is other.session), -1)
            if index < 0:
                raise RuntimeError("the second tab is not offered as side B")
            combo_b.setCurrentIndex(index)
        picker.key_combo.setEditText("id")
        picker.accept()
        if picker.error_text():
            raise RuntimeError(f"the compare dialog refused: {picker.error_text()}")
        compare = getattr(picker, "window_opened", None)
        if compare is None:
            raise RuntimeError("the compare window did not open")
        compare.resize(*WINDOW_SIZE)
        wait_until(lambda: compare.run_state() == "done", "the comparison")
        compare.next_change()                                           # the first changed record and its diff
        wait_until(lambda: not compare.view.is_loading() and compare.view.model() is not None, "the diff view")
        self.grab("compare", compare)
        compare.close()
        process(100)
        self.win.tabs.setCurrentWidget(other)
        self.win.close_tab()
        wait_until(lambda: other.session.state == "closed", "the second tab to close")
        self.win.tabs.setCurrentWidget(self.tab)
        process(100)

    def shot_stats(self) -> None:
        from PySide6.QtCore import Qt

        self.theme.apply_theme(self.qapp, "light")
        self.win.tabs.setCurrentWidget(self.tab)
        self.tab.set_second_pane_visible(False)     # tree + dock: with both panes the dock would be squeezed
        self.tree().collapse_all()                  # the record overview (deterministic whatever ran before)
        panel = self.win.show_stats("score", scope="all")
        if panel is None:
            raise RuntimeError("the statistics panel is not available")
        dock = self.win.docks["stats"]
        self.win.resizeDocks([dock], [420], Qt.Orientation.Horizontal)
        wait_until(lambda: panel.status() == "done", "the statistics of score")
        if hasattr(panel, "set_chart_kind") and "hist" in getattr(panel, "chart_kinds", lambda: [])():
            panel.set_chart_kind("hist")
        process(100)
        self.grab("stats")
        dock.hide()
        self.tab.set_second_pane_visible(True)
        self.win.resize(*WINDOW_SIZE)
        process(100)

    def shot_import_dialog(self) -> None:
        self.theme.apply_theme(self.qapp, "light")
        csv_path = self.files["csv"]
        cwd = os.getcwd()
        os.chdir(csv_path.parents[1])                   # the window shows the short relative path data\orders.csv
        try:
            dialog = self.win.show_import(csv_path.relative_to(csv_path.parents[1]))
            if dialog is None:
                raise RuntimeError("the import window did not open")
            dialog.resize(820, 640)
            process(300)
            self.grab("import-dialog", dialog)
            dialog.close()
            process(100)
        finally:
            os.chdir(cwd)

    def shot_palette(self) -> None:
        self.theme.apply_theme(self.qapp, "light")
        self.prepare_tree()                         # the main picture's tree and text pane behind the popup
        palette = self.win.show_palette()
        if palette is None:
            raise RuntimeError("the command palette did not open")
        palette.set_query("export")
        wait_until(lambda: palette.entries(), "palette entries")
        self.grab("palette")
        palette.close_palette()
        process(100)

    # -- end ---------------------------------------------------------------------------------------------------
    def close(self) -> None:
        if not self.win._closing:
            self.win.close()
        self.app.tasks.wait_idle(30)
        process(100)


def main(argv: list[str] | None = None) -> int:
    root = _root()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=root / "docs" / "img", help="folder of the PNG files")
    ap.add_argument("--work", type=Path, default=Path(tempfile.gettempdir()) / "jsonreshape-screenshots",
                    help="folder of the generated data and the private app home")
    ap.add_argument("--only", nargs="+", choices=SHOTS, metavar="NAME", help=f"only these pictures ({', '.join(SHOTS)})")
    ap.add_argument("--force", action="store_true", help="regenerate the data files")
    args = ap.parse_args(argv)
    work: Path = args.work.resolve()
    if args.force:
        for name in ("customers.json", "customers-2026-10.json", os.path.join("data", "orders.csv")):
            (work / name).unlink(missing_ok=True)
    os.environ["JSONRESHAPE_HOME"] = str(work / "home")
    (work / "home").mkdir(parents=True, exist_ok=True)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    files = make_data(work, root)
    out: Path = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    wanted = list(args.only or SHOTS)
    print(f"data: {files['customers']} ({files['customers'].stat().st_size / 1e6:.1f} MB), pictures -> {out}")

    shooter = Shooter(files, out)
    failed: list[str] = []
    try:
        for name in SHOTS:
            if name not in wanted:
                continue
            print(f"{name} ...")
            try:
                getattr(shooter, "shot_" + name.replace("-", "_"))()
            except Exception as exc:  # noqa: BLE001 - every picture is tried
                failed.append(name)
                print(f"  FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        shooter.close()
    for exc in shooter.errors:
        print(f"the program reported an error: {exc!r}", file=sys.stderr)
    too_big = [n for n, size in shooter.sizes.items() if size > MAX_BYTES]
    for name in too_big:
        print(f"{name}.png is {shooter.sizes[name] / 1024:.0f} KB (over {MAX_BYTES // 1024} KB)", file=sys.stderr)
    return 1 if failed or shooter.errors or too_big else 0


if __name__ == "__main__":
    sys.exit(main())
