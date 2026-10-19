"""Write the application icon as a multi-size ``.ico`` for the executables (build step of
``scripts/build_exe.ps1``): ``python packaging/make_icon.py build/jsonreshape.ico``.

The icon is :func:`jsonreshape.gui.app.make_app_icon` -- the one the running program shows -- rendered offscreen
in the sizes 16 ... 256 and stored as PNG images inside the ICO container (Windows Vista and later read them).
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

SIZES = (16, 24, 32, 48, 64, 128, 256)          # the sizes make_app_icon() draws


def ico_bytes(images: list[tuple[int, bytes]]) -> bytes:
    """An ICO file of ``(size, PNG bytes)`` square images."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, blobs = [], []
    for size, png in images:
        dim = 0 if size >= 256 else size            # 0 means 256 in the directory entry
        entries.append(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset))
        blobs.append(png)
        offset += len(png)
    return header + b"".join(entries) + b"".join(blobs)


def render(sizes: tuple[int, ...] = SIZES) -> list[tuple[int, bytes]]:
    """PNG bytes of the application icon in ``sizes`` (paints into pixmaps: no window is shown). The offscreen
    platform has no fonts of its own: it then reads the system's font folder."""
    if os.environ.get("QT_QPA_PLATFORM", "").startswith("offscreen") and not os.environ.get("QT_QPA_FONTDIR"):
        fonts = Path(os.environ.get("WINDIR") or "C:\\Windows") / "Fonts"
        if fonts.is_dir():
            os.environ["QT_QPA_FONTDIR"] = str(fonts)
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize
    from PySide6.QtGui import QGuiApplication

    from jsonreshape.gui.app import make_app_icon

    app = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])
    icon = make_app_icon()
    out = []
    for size in sizes:
        image = icon.pixmap(QSize(size, size)).toImage()
        if image.width() != size or image.height() != size:
            from PySide6.QtCore import Qt

            image = image.scaled(size, size, Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        if not image.save(buf, "PNG"):
            raise RuntimeError(f"could not encode the {size}px icon")
        buf.close()
        out.append((size, bytes(data.data())))
    del app
    return out


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        sys.stderr.write("usage: make_icon.py OUTPUT.ico\n")
        return 2
    target = Path(args[0])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(ico_bytes(render()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
