"""Render the GitHub social-preview card (1280x640) to ``docs/img/social-preview.png``.

Upload the result in the repository Settings -> General -> Social preview; GitHub then
shows it when a link to the repo is shared on Telegram / X / LinkedIn / Slack etc.

The card is drawn with QPainter (PySide6) rather than from an SVG: QSvgRenderer renders
``<text>`` as missing-glyph boxes under the "offscreen" platform, and the offscreen
platform does not expose the system font database, so the faces are loaded explicitly
with ``QFontDatabase.addApplicationFont``. Run it like the screenshots:

    QT_QPA_PLATFORM=offscreen .venv/Scripts/python.exe scripts/make_social_preview.py

Requires PySide6 (already a project dependency). Windows is assumed for the font paths;
on another OS pass equivalent .ttf paths via --sans / --sans-bold / --mono / --mono-bold.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QColor, QFont, QFontDatabase, QFontMetrics, QGuiApplication, QImage,
    QLinearGradient, QPainter, QPen,
)

REPO = Path(__file__).resolve().parent.parent
W, H = 1280, 640
FONTS = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--out", default=str(REPO / "docs" / "img" / "social-preview.png"))
    ap.add_argument("--sans", default=str(FONTS / "segoeui.ttf"))
    ap.add_argument("--sans-bold", default=str(FONTS / "segoeuib.ttf"))
    ap.add_argument("--mono", default=str(FONTS / "consola.ttf"))
    ap.add_argument("--mono-bold", default=str(FONTS / "consolab.ttf"))
    args = ap.parse_args()

    app = QGuiApplication([])  # noqa: F841 (needed for the font database / painter)

    sans = _load(args.sans, args.sans_bold, fallback="Arial")
    mono = _load(args.mono, args.mono_bold, fallback="Courier New")

    img = QImage(W, H, QImage.Format_ARGB32)
    img.fill(QColor("#0b1220"))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)

    def font(size: int, weight=QFont.Normal, family: str = sans) -> QFont:
        f = QFont(family)
        f.setPixelSize(size)
        f.setWeight(weight)
        return f

    # background gradient
    bg = QLinearGradient(0, 0, W, H)
    bg.setColorAt(0, QColor("#0b1220"))
    bg.setColorAt(1, QColor("#15243d"))
    p.fillRect(0, 0, W, H, bg)

    # top accent bar
    bar = QLinearGradient(0, 0, W, 0)
    bar.setColorAt(0, QColor("#5aa2ff"))
    bar.setColorAt(1, QColor("#34d399"))
    p.fillRect(0, 0, W, 7, bar)

    # subtle brace motif on the right
    p.setPen(QColor("#182b49"))
    p.setFont(font(420, QFont.Bold, mono))
    brace = "{ }"
    p.drawText(1130 - QFontMetrics(p.font()).horizontalAdvance(brace) // 2, 470, brace)

    # repo slug
    p.setPen(QColor("#8aa0bd"))
    p.setFont(font(24, QFont.Normal, mono))
    p.drawText(80, 92, "github.com/Erkinbek/jsonreshape")

    # title
    p.setPen(QColor("#eaf1fb"))
    p.setFont(font(108, QFont.Bold))
    p.drawText(76, 212, "jsonreshape")

    # taglines
    p.setPen(QColor("#cdd9ea"))
    p.setFont(font(37, QFont.DemiBold))
    p.drawText(80, 272, "Open, search & reshape huge JSON files")
    p.setPen(QColor("#8aa0bd"))
    p.setFont(font(26, QFont.Normal))
    p.drawText(80, 316, "GB-scale \u2014 the file is never loaded into memory, the original is never changed")

    # format chips
    chips = [
        ("JSON", 80, 94), ("JSON Lines", 186, 150), ("CSV", 348, 86),
        ("XLSX", 446, 92), ("SQLite", 550, 106), ("Parquet", 668, 118),
    ]
    p.setFont(font(23, QFont.Normal))
    for label, x, w in chips:
        p.setPen(QPen(QColor("#2c415f"), 1))
        p.setBrush(QColor("#17293f"))
        p.drawRoundedRect(QRectF(x, 366, w, 46), 23, 23)
        p.setPen(QColor("#c6d8f0"))
        p.drawText(QRectF(x, 366, w, 46), Qt.AlignCenter, label)

    # divider
    p.setPen(QPen(QColor("#223850"), 2))
    p.drawLine(80, 477, 1080, 477)

    # metrics (mixed colours, laid out left to right)
    amber, grey, sep = QColor("#fbbf24"), QColor("#8aa0bd"), QColor("#415873")
    segments = [
        ("2 GB", amber, QFont.Bold), (" indexed in ~3 s", grey, QFont.Normal),
        ("      \u00b7      ", sep, QFont.Normal),
        ("10M", amber, QFont.Bold), (" records sorted in 77 s", grey, QFont.Normal),
        ("      \u00b7      ", sep, QFont.Normal),
        ("export up to ", grey, QFont.Normal), ("220 MB/s", amber, QFont.Bold),
    ]
    x = 80
    for text, color, weight in segments:
        f = font(28, weight)
        p.setFont(f)
        p.setPen(color)
        p.drawText(x, 536, text)
        x += QFontMetrics(f).horizontalAdvance(text)

    # footer
    p.setPen(QColor("#6f87a6"))
    p.setFont(font(23, QFont.Normal))
    p.drawText(80, 590, "Windows \u00b7 Linux \u00b7 macOS   \u2014   GUI + CLI \u00b7 Rust core \u00b7 no telemetry, fully offline \u00b7 Apache-2.0")

    p.end()
    ok = img.save(args.out, "PNG")
    print("saved" if ok else "FAILED", args.out, f"{W}x{H}")
    return 0 if ok else 1


def _load(regular: str, bold: str, *, fallback: str) -> str:
    """Register the two faces and return the resolved family name (or a fallback)."""
    family = fallback
    for i, path in enumerate((regular, bold)):
        fid = QFontDatabase.addApplicationFont(path)
        if fid != -1 and i == 0:
            fams = QFontDatabase.applicationFontFamilies(fid)
            if fams:
                family = fams[0]
    return family


if __name__ == "__main__":
    sys.exit(main())
