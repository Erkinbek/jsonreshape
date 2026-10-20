"""Windows version resources of the two executables (used by ``packaging/jsonreshape.spec``).

The version comes from ``jsonreshape/__init__.py`` (``__version__``, read as text: the build does not import the
package for it). ``FileVersion`` / ``ProductVersion`` keep the text (``0.1.0``); the fixed numeric version takes
its first four numbers (``0.1.0`` -> ``0.1.0.0``; a suffix such as ``rc1`` is ignored there).

``FileDescription`` is what Windows shows as the program's name (Task Manager, "Open with", the file's
properties), so the GUI's is the product name.
"""

from __future__ import annotations

import re
from pathlib import Path

PRODUCT = "JSON Reshape"
COMPANY = "Erkin Pardayev"
COPYRIGHT = "© 2026 Erkin Pardayev"

_VERSION_RE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.MULTILINE)


def app_version(root: str | Path) -> str:
    """``__version__`` of ``<root>/jsonreshape/__init__.py``."""
    text = (Path(root) / "jsonreshape" / "__init__.py").read_text(encoding="utf-8")
    match = _VERSION_RE.search(text)
    if match is None:
        raise ValueError("no __version__ in jsonreshape/__init__.py")
    return match.group(1)


def version_tuple(version: str) -> tuple[int, int, int, int]:
    """The four numbers of the fixed version: the leading ``N.N.N.N`` part (``2.0rc1`` -> ``2.0.0.0``; missing
    numbers are 0; each at most 65535)."""
    match = re.match(r"\s*v?(\d+(?:\.\d+)*)", version)
    if match is None:
        raise ValueError(f"not a version: {version!r}")
    numbers = [int(n) for n in match.group(1).split(".")[:4]]
    numbers += [0] * (4 - len(numbers))
    if any(n > 0xFFFF for n in numbers):
        raise ValueError(f"version number too large for a Windows version resource: {version!r}")
    return numbers[0], numbers[1], numbers[2], numbers[3]


def version_strings(root: str | Path, *, description: str, internal_name: str,
                    original_filename: str) -> dict[str, str]:
    """The string table of one executable."""
    version = app_version(root)
    return {
        "CompanyName": COMPANY,
        "FileDescription": description,
        "FileVersion": version,
        "InternalName": internal_name,
        "LegalCopyright": COPYRIGHT,
        "OriginalFilename": original_filename,
        "ProductName": PRODUCT,
        "ProductVersion": version,
    }


def version_resource(root: str | Path, *, description: str, internal_name: str, original_filename: str):
    """A PyInstaller ``VSVersionInfo`` for ``EXE(version=...)``."""
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    numbers = version_tuple(app_version(root))
    strings = version_strings(root, description=description, internal_name=internal_name,
                              original_filename=original_filename)
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1,
                          subtype=0x0, date=(0, 0)),
        kids=[
            StringFileInfo([StringTable("040904B0", [StringStruct(k, v) for k, v in strings.items()])]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )
