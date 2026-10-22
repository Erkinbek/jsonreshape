"""The demo GIF of the README (``docs/img/demo.gif``) -- a developer tool, not part of the program and not collected
by pytest.

Run from the project root (the venv with PySide6 and the package installed, ``ffmpeg`` on the PATH)::

    .venv\\Scripts\\python scripts\\make_demo_gif.py
    .venv\\Scripts\\python scripts\\make_demo_gif.py --mp4 C:\\tmp\\demo.mp4      # also a video for the announcements
    .venv\\Scripts\\python scripts\\make_demo_gif.py --data D:\\big.json --keep-frames

The real window is started offscreen exactly as :mod:`make_screenshots` starts it (English texts, the Windows UI
font, a private ``JSONRESHAPE_HOME``) and driven through its own methods on a large synthetic file:
``bench/data/syn_2g.json`` (2.1 GB, 6.5 million records), written by ``bench/make_synthetic.py --size 2GB`` when it is
missing (about 40 s). Five scenes are recorded:

1. opening the file -- the first records at once, the index built in the background;
2. browsing a record in the tree with its text in the second pane;
3. a search over every record (byte mode);
4. a filter expression;
5. the export of the filtered records to CSV (next to the data file, a relative name) and its report.

Every frame is ``QWidget.grab()`` of the main window (an open dialog is drawn centred over it) with a caption strip
below it: the scene, a line about it and a clock. **The clock is real time** -- the frames of the opening, the search,
the filter and the export are taken while the work runs and each frame stays on screen for as long as it really
took, except the filter, which is shown :data:`FILTER_SPEED` times faster and says so under the clock; only the typing
and the pauses between the scenes are staged. The frames are PNG files in the work folder; ``ffmpeg`` turns them into
the GIF (one 256-colour palette for the whole film, no dithering: flat UI colours stay exact; each frame stores only
the rectangle that changed).

Exit code 1 when a scene failed (the reason is printed), the program reported an error or ``ffmpeg`` failed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_screenshots as shots  # noqa: E402 - sets the Qt environment before Qt is imported

WINDOW_SIZE = (1120, 700)
STRIP_HEIGHT = 58
DOCK_WIDTH = 540            # the search and filter docks, on the right of the tree
FRAME_MS = 90               # the pause between two frames of a running task (plus the time a grab takes)
TYPE_MS = 70                # one typed character
MIN_FRAME_MS = 30           # browsers show shorter GIF frames for 100 ms
SEARCH_TEXT = "Toshkent"
FILTER_TEXT = 'score > 990 and contact.address.city == "Samarqand"'
FILTER_SPEED = 8            # the filter reads every record (~25 s on 2 GB): shown this many times faster
EXPORT_NAME = "samarqand-top.csv"
MAX_GIF_BYTES = 6 * 1024 * 1024

STRIP_BG = "#1d2330"
STRIP_TITLE = "#ffffff"
STRIP_TEXT = "#aab4c8"
STRIP_ACCENT = "#4c8dff"
STRIP_CLOCK = "#7ee2a8"


# ======================================================================================================== frames
class Recorder:
    """The frames: the window (and an open dialog) with the caption strip, written as PNG files."""

    def __init__(self, win: Any, folder: Path) -> None:
        self.win = win
        self.folder = folder
        # {"path", "t": monotonic time of the grab, "ms": fixed duration or None (real time), "speed": its playback speed}
        self.frames: list[dict[str, Any]] = []
        self.speed = 1
        self.step = 0
        self.title = ""
        self.line = ""
        self.clock_start: float | None = None
        self.clock_text = ""
        self.dialogs: list[Any] = []

    # -- caption and clock --------------------------------------------------------------------------------------
    def scene(self, step: int, title: str, line: str) -> None:
        self.step, self.title, self.line = step, title, line
        self.clock_start, self.clock_text = None, ""

    def start_clock(self) -> float:
        self.clock_start = time.monotonic()
        self.clock_text = "0.0 s"
        return self.clock_start

    def stop_clock(self, text: str) -> float:
        elapsed = time.monotonic() - (self.clock_start or time.monotonic())
        self.clock_start = None
        self.clock_text = text
        return elapsed

    # -- grabbing -----------------------------------------------------------------------------------------------
    def snap(self, ms: int | None = None) -> None:
        """One frame now; ``ms``: how long it is shown (``None``: until the next frame, as in real time)."""
        shots.process(15)
        if self.clock_start is not None:
            self.clock_text = f"{time.monotonic() - self.clock_start:.1f} s"
        image = self._compose()
        path = self.folder / f"f{len(self.frames):05d}.png"
        from PySide6.QtGui import QImageWriter

        writer = QImageWriter(str(path), b"png")
        writer.setCompression(10)                       # fast: these are intermediate files
        if not writer.write(image):
            raise RuntimeError(f"could not write {path}: {writer.errorString()}")
        self.frames.append({"path": path, "t": time.monotonic(), "ms": ms, "speed": self.speed})

    def hold(self, ms: int) -> None:
        self.snap(ms)

    def _compose(self) -> Any:
        from PySide6.QtCore import QRect, Qt
        from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen

        width, height = self.win.width(), self.win.height()
        image = QImage(width, height + STRIP_HEIGHT, QImage.Format.Format_RGB32)
        image.fill(QColor(STRIP_BG))
        p = QPainter(image)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.drawPixmap(0, 0, self.win.grab())
        for dialog in self.dialogs:
            if dialog is None or not dialog.isVisible():
                continue
            pix = dialog.grab()
            x = max(0, (width - pix.width()) // 2)
            y = max(0, (height - pix.height()) // 2)
            for spread, alpha in ((10, 18), (6, 26), (3, 40)):          # a soft shadow
                p.fillRect(QRect(x - spread, y - spread + 4, pix.width() + 2 * spread, pix.height() + 2 * spread),
                           QColor(0, 0, 0, alpha))
            p.drawPixmap(x, y, pix)
            p.setPen(QPen(QColor(0, 0, 0, 90), 1))
            p.drawRect(x, y, pix.width() - 1, pix.height() - 1)
        # the caption strip
        top = height
        p.fillRect(0, top, width, STRIP_HEIGHT, QColor(STRIP_BG))
        p.fillRect(0, top, width, 3, QColor(STRIP_ACCENT))
        if self.step:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(STRIP_ACCENT))
            p.drawEllipse(16, top + 16, 28, 28)
            p.setPen(QColor("#ffffff"))
            font = QFont("Segoe UI")
            font.setPixelSize(15)
            font.setBold(True)
            p.setFont(font)
            p.drawText(QRect(16, top + 16, 28, 28), Qt.AlignmentFlag.AlignCenter, str(self.step))
        left = 56 if self.step else 16
        font = QFont("Segoe UI")
        font.setPixelSize(16)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor(STRIP_TITLE))
        clock_w = 300
        p.drawText(QRect(left, top + 9, width - left - clock_w, 22), Qt.AlignmentFlag.AlignLeft
                   | Qt.AlignmentFlag.AlignVCenter, self.title)
        font = QFont("Segoe UI")
        font.setPixelSize(13)
        p.setFont(font)
        p.setPen(QColor(STRIP_TEXT))
        p.drawText(QRect(left, top + 31, width - left - clock_w, 20), Qt.AlignmentFlag.AlignLeft
                   | Qt.AlignmentFlag.AlignVCenter, self.line)
        if self.clock_text:
            running = self.clock_start is not None
            font = QFont("Consolas")
            font.setPixelSize(20 if running else 15)
            font.setBold(True)
            p.setFont(font)
            p.setPen(QColor(STRIP_CLOCK))
            fast = running and self.speed > 1
            box = QRect(width - clock_w - 16, top + (5 if fast else 6), clock_w, 28 if fast else STRIP_HEIGHT - 9)
            p.drawText(box, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self.clock_text)
            if fast:
                font = QFont("Segoe UI")
                font.setPixelSize(12)
                p.setFont(font)
                p.setPen(QColor(STRIP_TEXT))
                p.drawText(QRect(width - clock_w - 16, top + 33, clock_w, 18), Qt.AlignmentFlag.AlignRight
                           | Qt.AlignmentFlag.AlignVCenter, f"real time, shown {self.speed}× faster")
        p.end()
        return image

    # -- the film -----------------------------------------------------------------------------------------------
    def durations(self) -> list[int]:
        out = []
        for i, frame in enumerate(self.frames):
            if frame["ms"] is not None:
                ms = frame["ms"]
            elif i + 1 < len(self.frames):
                ms = int(round((self.frames[i + 1]["t"] - frame["t"]) * 1000 / frame["speed"]))
            else:
                ms = 1000
            out.append(max(MIN_FRAME_MS, ms))
        return out

    def write_concat(self) -> Path:
        """The ffmpeg concat list of the frames with their durations."""
        lines = ["ffconcat version 1.0"]
        for frame, ms in zip(self.frames, self.durations()):
            lines.append(f"file '{frame['path'].name}'")
            lines.append(f"duration {ms / 1000:.3f}")
        lines.append(f"file '{self.frames[-1]['path'].name}'")     # the concat demuxer ignores the last duration
        path = self.folder / "frames.txt"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


# ======================================================================================================== scenes
class Demo:
    def __init__(self, data: Path, work: Path, frames: Path) -> None:
        from PySide6.QtGui import QFont, QFontDatabase
        from PySide6.QtWidgets import QApplication

        from jsonreshape.gui import app as app_mod
        from jsonreshape.gui import theme

        self.data = data
        self.work = work
        self.errors: list[BaseException] = []
        self.timings: dict[str, float] = {}
        qapp = QApplication.instance() or QApplication([sys.argv[0]])
        installed = set(QFontDatabase.families())
        family = next((f for f in shots.UI_FONT_FAMILIES if f in installed), None)
        if family is not None:
            qapp.setFont(QFont(family, shots.UI_FONT_POINTS))
        self.win = app_mod.main([], run=False)
        if not hasattr(self.win, "tab_widgets"):
            raise RuntimeError(f"the window did not start: {self.win!r}")
        self.qapp = QApplication.instance()
        self.app = self.win.app
        self.app.headless = True
        self.app.tasks.set_poll_interval(20)
        self.app.error_reported.connect(self.errors.append)
        theme.apply_theme(self.qapp, "light")
        self.win.resize(*WINDOW_SIZE)
        shots.process(400)
        self.rec = Recorder(self.win, frames)
        self.tab: Any = None

    def _size_ok(self) -> None:
        if (self.win.width(), self.win.height()) != WINDOW_SIZE:
            self.win.resize(*WINDOW_SIZE)
            shots.process(200)

    def run_while(self, busy: Any, *, speed: int = 1, timeout: float = shots.TIMEOUT_S) -> None:
        """Frames while ``busy()`` is true, each shown for the time until the next one divided by ``speed`` (one
        frame every :data:`FRAME_MS` of the film)."""
        end = time.monotonic() + timeout
        self.rec.speed = speed
        try:
            while busy():
                if time.monotonic() > end:
                    raise TimeoutError("the task did not end in time")
                shots.process(FRAME_MS * speed)
                self.rec.snap()
        finally:
            self.rec.speed = 1

    def dock_right(self, name: str) -> Any:
        """The dock ``name`` on the right of the tree (the second pane hidden), :data:`DOCK_WIDTH` wide."""
        from PySide6.QtCore import Qt

        self.tab.set_second_pane_visible(False)
        dock = self.win.docks[name]
        self.win.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        dock.show()
        self.win.resizeDocks([dock], [DOCK_WIDTH], Qt.Orientation.Horizontal)
        shots.process(100)
        return dock

    # -- 1 ------------------------------------------------------------------------------------------------------
    def scene_open(self) -> None:
        rec = self.rec
        size_gb = self.data.stat().st_size / 1e9
        rec.scene(1, f"Open a {size_gb:.1f} GB JSON file",
                  "The first records show at once; the index of every record is built in the background.")
        rec.hold(1200)
        t0 = rec.start_clock()
        self.tab = self.win.open_path(self.data)
        if self.tab is None:
            raise RuntimeError(f"{self.data} did not open")
        session = self.tab.session
        first: list[float] = []

        def busy() -> bool:
            if not first and self.tab.panes and self._first_row_shown():
                first.append(time.monotonic() - t0)
            return session.state not in ("ready", "failed")

        self.run_while(busy)
        if session.state != "ready":
            raise RuntimeError(f"{self.data} did not open: {session.open_error}")
        elapsed = time.monotonic() - t0
        count = session.doc.record_count
        self.timings.update(open_first_rows=first[0] if first else float("nan"), open_index=elapsed)
        rec.stop_clock(f"{count:,} records · {elapsed:.1f} s")
        rec.hold(2600)

    def _first_row_shown(self) -> bool:
        try:
            tree = self.tree()
            return tree.cell_text(0, "key") is not None
        except Exception:  # noqa: BLE001 - not built yet
            return False

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

    # -- 2 ------------------------------------------------------------------------------------------------------
    def scene_browse(self) -> None:
        rec = self.rec
        rec.scene(2, "Browse it like a small file",
                  "Tree, table and text views; only the records on screen are read from the disk.")
        tree = self.tree()
        text = self.text_view()
        keys = ("id", "name", "active", "score", "created_at", "contact", "tags", "orders", "children", "note")
        shots.expand(tree, (1,))
        rec.hold(700)
        shots.expand(tree, (1, keys.index("contact")))
        shots.expand(tree, (1, keys.index("contact"), 1))
        rec.hold(600)
        shots.expand(tree, (1, keys.index("orders")))
        row = tree.row_of((1,))
        if row is not None:
            tree.select_row(row)
        shots.wait_until(lambda: not text.is_loading() and '"name"' in text.text(), "the text pane")
        tree.grid.setFocus()
        rec.hold(1800)

    # -- 3 ------------------------------------------------------------------------------------------------------
    def scene_search(self) -> None:
        rec = self.rec
        rec.scene(3, "Search every record",
                  "A byte-level scan of the whole file; the first matches show at once.")
        self.tree().collapse_all()
        panel = self.win.show_search("")
        self.dock_right("search")
        panel.set_options({"mode": "plain", "case_sensitive": False, "whole_word": False, "translit": False,
                           "scope": "both", "path": ""})
        rec.hold(500)
        for i in range(1, len(SEARCH_TEXT) + 1):
            panel.set_query_text(SEARCH_TEXT[:i])
            rec.snap(TYPE_MS)
        rec.hold(400)
        t0 = rec.start_clock()
        if panel.run_search() is None:
            raise RuntimeError(f"the search did not start: {panel.error_text()}")
        self.run_while(panel.is_running)
        elapsed = time.monotonic() - t0
        self.timings["search"] = elapsed
        state = panel.state()
        result = getattr(state, "result", None)
        hits = getattr(result, "hit_count", None) or panel.hit_count_shown()
        rec.stop_clock(f"{hits:,} matches · {elapsed:.1f} s")
        rec.hold(1400)
        if panel.hit_count_shown():                     # the tree goes to the first match
            panel.activate_hit(0)
        rec.hold(2200)

    # -- 4 ------------------------------------------------------------------------------------------------------
    def scene_filter(self) -> None:
        rec = self.rec
        rec.scene(4, "Filter with an expression",
                  "Comparisons, and / or / not, regular expressions, nested paths — or JMESPath.")
        self.win.docks["search"].hide()
        self.tree().collapse_all()
        panel = self.win.show_filter()
        self.dock_right("filter")
        editor = panel.current_editor()
        session = self.tab.session
        rec.hold(500)
        step = 3
        for i in range(step, len(FILTER_TEXT) + step, step):
            editor.set_text(FILTER_TEXT[:i])
            rec.snap(TYPE_MS)
        rec.hold(500)
        t0 = rec.start_clock()
        if not panel.apply():
            raise RuntimeError(f"the filter did not start: {panel.status_text()}")
        self.run_while(lambda: session.view_busy, speed=FILTER_SPEED)
        elapsed = time.monotonic() - t0
        self.timings["filter"] = elapsed
        rec.stop_clock(f"{session.view_len():,} records · {elapsed:.1f} s")
        rec.hold(2400)

    # -- 5 ------------------------------------------------------------------------------------------------------
    def scene_export(self) -> None:
        rec = self.rec
        rec.scene(5, "Export the result",
                  "CSV, Excel, SQLite, Parquet, Feather, JSON Lines — streamed, the file is never loaded whole.")
        self.export_path = self.data.parent / EXPORT_NAME
        self.export_path.unlink(missing_ok=True)
        dialog = self.win.show_export(mapping=False)
        if dialog is None:
            raise RuntimeError("the export dialog did not open")
        rec.dialogs = [dialog]
        dialog.set_format("csv")
        dialog.set_scope("view")
        dialog.set_output_path(EXPORT_NAME)             # relative: next to the data file, a short name on screen
        shots.process(300)
        rec.hold(2000)
        finished: list[Any] = []
        dialog.export_finished.connect(finished.append)
        t0 = rec.start_clock()
        if dialog.start_export() is None:
            raise RuntimeError(f"the export did not start: {dialog.error_text()}")
        self.run_while(lambda: not finished)
        elapsed = time.monotonic() - t0
        self.timings["export"] = elapsed
        outcome = finished[0]
        report = getattr(outcome, "report", None)
        rows = getattr(report, "rows", None) or getattr(report, "records", 0)
        rec.stop_clock(f"{rows:,} rows · {elapsed:.1f} s")
        shots.process(300)
        from PySide6.QtWidgets import QApplication, QDialog

        shown = [w for w in QApplication.topLevelWidgets()
                 if isinstance(w, QDialog) and w.isVisible() and w is not dialog]
        rec.dialogs = shown[-1:] or [dialog]
        rec.hold(3200)
        for w in shown:
            w.close()
        if dialog.isVisible():
            dialog.close()
        rec.dialogs = []

    def close(self) -> None:
        if not self.win._closing:
            self.win.close()
        self.app.tasks.wait_idle(30)
        shots.process(100)


# ========================================================================================================= ffmpeg
def find_ffmpeg(given: str | None) -> str:
    for candidate in (given, shutil.which("ffmpeg")):
        if candidate and Path(candidate).is_file():
            return candidate
    raise SystemExit("ffmpeg was not found: put it on the PATH or pass --ffmpeg PATH")


def make_gif(ffmpeg: str, concat: Path, out: Path) -> None:
    graph = ("split[a][b];[a]palettegen=max_colors=256:stats_mode=full[p];"
             "[b][p]paletteuse=dither=none:diff_mode=rectangle")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part.gif")
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(concat), "-vf", graph, "-fps_mode", "vfr", "-loop", "0", str(tmp)], check=True)
    os.replace(tmp, out)


def make_mp4(ffmpeg: str, concat: Path, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(concat), "-vf", "fps=30,pad=ceil(iw/2)*2:ceil(ih/2)*2", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-crf", "18", "-movflags", "+faststart", str(out)], check=True)


# =========================================================================================================== main
def main(argv: list[str] | None = None) -> int:
    root = shots._root()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=root / "docs" / "img" / "demo.gif", help="the GIF file")
    ap.add_argument("--mp4", type=Path, help="also write the film as an MP4 video here")
    ap.add_argument("--data", type=Path, default=root / "bench" / "data" / "syn_2g.json",
                    help="the JSON file shown (generated with bench/make_synthetic.py --size 2GB when missing)")
    ap.add_argument("--work", type=Path, default=Path(tempfile.gettempdir()) / "jsonreshape-demo",
                    help="folder of the frames, the export and the private app home")
    ap.add_argument("--ffmpeg", help="the ffmpeg program (default: from the PATH)")
    ap.add_argument("--keep-frames", action="store_true", help="keep the PNG frames in the work folder")
    args = ap.parse_args(argv)
    ffmpeg = find_ffmpeg(args.ffmpeg)
    work: Path = args.work.resolve()
    frames = work / "frames"
    if frames.exists():
        shutil.rmtree(frames)
    frames.mkdir(parents=True)
    home = work / "home"
    if home.exists():
        shutil.rmtree(home)                     # a clean start: no restored session, no search history
    home.mkdir(parents=True)
    os.environ["JSONRESHAPE_HOME"] = str(home)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    data: Path = args.data.resolve()
    if not data.is_file():
        print(f"writing {data} (2 GB of synthetic records) ...")
        data.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, str(root / "bench" / "make_synthetic.py"), "--size", "2GB",
                        "--out", str(data), "-q"], check=True)
    print(f"data: {data} ({data.stat().st_size / 1e9:.2f} GB), frames -> {frames}")

    demo = Demo(data, work, frames)
    failed: list[str] = []
    try:
        for name in ("open", "browse", "search", "filter", "export"):
            print(f"{name} ...")
            try:
                demo._size_ok()
                getattr(demo, "scene_" + name)()
            except Exception as exc:  # noqa: BLE001 - reported, the film stops here
                failed.append(name)
                print(f"  FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
                break
    finally:
        demo.close()
        if getattr(demo, "export_path", None) is not None:
            demo.export_path.unlink(missing_ok=True)
    for exc in demo.errors:
        print(f"the program reported an error: {exc!r}", file=sys.stderr)
    for key, value in demo.timings.items():
        print(f"  {key}: {value:.2f} s")
    rec = demo.rec
    if failed or demo.errors or not rec.frames:
        return 1
    total = sum(rec.durations()) / 1000
    print(f"{len(rec.frames)} frames, {total:.1f} s")
    concat = rec.write_concat()
    make_gif(ffmpeg, concat, args.out.resolve())
    size = args.out.stat().st_size
    print(f"{args.out}: {size / 1024 / 1024:.2f} MB")
    if args.mp4:
        make_mp4(ffmpeg, concat, args.mp4.resolve())
        print(f"{args.mp4}: {args.mp4.stat().st_size / 1024 / 1024:.2f} MB")
    if not args.keep_frames:
        shutil.rmtree(frames, ignore_errors=True)
    if size > MAX_GIF_BYTES:
        print(f"the GIF is over {MAX_GIF_BYTES // 1024 // 1024} MB", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
