# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec of jsonreshape (docs/ARCHITECTURE.md §15.0 ".exe"): a ONEDIR build.

    dist\\jsonreshape\\jsonreshape.exe        the CLI (console)
    dist\\jsonreshape\\jsonreshape-gui.exe    the viewer (windowed)
    dist\\jsonreshape\\_internal\\            shared: Python, the Rust core (.pyd), PySide6 / Qt, libraries

ONEDIR, not ONEFILE: a one-file exe unpacks ~150 MB into a temporary folder at every start. Build it with
``scripts/build_exe.ps1`` (or ``python -m PyInstaller --noconfirm --clean --distpath dist --workpath
build/pyinstaller packaging/jsonreshape.spec`` from the project root, after ``python packaging/make_icon.py
build/jsonreshape.ico``).

* Each exe has its own Python archive (the CLI's has no Qt and no ``jsonreshape.gui``); both share ``_internal``.
* Every ``jsonreshape`` module is included (the GUI imports its views and dialogs by name), the ijson C backend
  (``yajl2_c``: chosen at run time), and the metadata of the libraries (versions in "About" / ``--version``).
* Qt: QtCore, QtGui, QtWidgets with the ``platforms``, ``styles``, ``imageformats`` and ``iconengines`` plugins.
  Not shipped: QtNetwork and everything that needs it (the program is offline), Qml / Quick, WebEngine, 3D,
  multimedia, PDF (the ``qpdf`` image plugin), the virtual-keyboard and TUIO-touch plugins (they pull in Qml /
  QtNetwork), the software OpenGL renderer (``opengl32sw.dll``, 20 MB: no view uses OpenGL) and the Qt
  translations (the program has its own texts and loads no Qt translation). Also not: ``ssl`` (no TLS),
  tkinter, test packages, build tools.
* A version resource from ``jsonreshape.__version__`` (``packaging/version_info.py``) and the icon of
  ``jsonreshape.gui.app.make_app_icon`` (``build/jsonreshape.ico``, when present).
"""

import os
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

HERE = Path(SPECPATH).resolve()          # noqa: F821 - defined by PyInstaller
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
from version_info import version_resource  # noqa: E402

ICON = Path(os.environ.get("JSONRESHAPE_ICON") or ROOT / "build" / "jsonreshape.ico")
ICON_ARG = [str(ICON)] if ICON.is_file() else None

IJSON = ["ijson.backends.yajl2_c", "ijson.backends._yajl2", "ijson.backends.python"]
GUI_HIDDEN = collect_submodules("jsonreshape") + IJSON
CLI_HIDDEN = collect_submodules("jsonreshape", filter=lambda name: not name.startswith("jsonreshape.gui")) + IJSON

QT_UNUSED = [f"PySide6.{m}" for m in (
    "QtNetwork", "QtNetworkAuth", "QtQml", "QtQuick", "QtQuickWidgets", "QtQuickControls2", "QtQuick3D",
    "QtQuickTest", "QtWebEngineCore", "QtWebEngineWidgets", "QtWebEngineQuick", "QtWebChannel", "QtWebSockets",
    "QtWebView", "QtHttpServer", "Qt3DCore", "Qt3DRender", "Qt3DInput", "Qt3DLogic", "Qt3DAnimation", "Qt3DExtras",
    "QtMultimedia", "QtMultimediaWidgets", "QtSpatialAudio", "QtTextToSpeech", "QtPdf", "QtPdfWidgets", "QtCharts",
    "QtDataVisualization", "QtGraphs", "QtGraphsWidgets", "QtLocation", "QtPositioning", "QtSensors", "QtSerialPort",
    "QtSerialBus", "QtBluetooth", "QtNfc", "QtRemoteObjects", "QtScxml", "QtStateMachine", "QtSql", "QtTest",
    "QtDesigner", "QtHelp", "QtUiTools", "QtOpenGL", "QtOpenGLWidgets", "QtPrintSupport", "QtSvgWidgets", "QtXml",
    "QtConcurrent", "QtDBus", "QtAxContainer")]
#: ``ssl``: no TLS in an offline program (the standard library's ``http.client`` / ``urllib.request`` /
#: ``ftplib`` / ``smtplib`` -- in the archive only because other standard modules refer to them -- import it
#: optionally).
EXCLUDES = ["tkinter", "_tkinter", "test", "tests", "pytest", "_pytest", "pytestqt", "hypothesis", "jsonschema",
            "setuptools", "pkg_resources", "pip", "PyInstaller", "IPython", "psutil.tests", "ssl", "_ssl"] + QT_UNUSED

METADATA = []
for dist in ("orjson", "ijson", "XlsxWriter", "jmespath", "psutil", "PySide6", "PySide6_Essentials", "shiboken6"):
    try:
        METADATA += copy_metadata(dist)
    except Exception:  # a library installed without metadata: its __version__ is used instead
        pass

#: Collected files (destination paths) that are not shipped -- see the module docstring.
_DROP = [re.compile(p, re.IGNORECASE) for p in (
    r"(^|/)opengl32sw\.dll$",
    r"(^|/)qt6(network|qml|quick|webengine|webchannel|websockets|3d|multimedia|spatialaudio|texttospeech|pdf"
    r"|virtualkeyboard|positioning|location|sensors|serialport|serialbus|bluetooth|nfc|charts|datavisualization"
    r"|graphs|opengl|printsupport|sql|test|designer|help|uitools|statemachine|scxml|remoteobjects|httpserver"
    r"|labs|shadertools|concurrent|dbus|xml)\w*\.dll$",
    r"(^|/)plugins/(tls|networkinformation|qmltooling|multimedia|position|sensors|webview|sceneparsers|renderers"
    r"|renderplugins|assetimporters|geometryloaders|geoservices|texttospeech|canbus|designer|sqldrivers"
    r"|scxmldatamodel|vectorimageformats|printsupport)/",
    r"(^|/)plugins/imageformats/qpdf\.dll$",
    r"(^|/)plugins/generic/qtuiotouchplugin\.dll$",
    r"(^|/)plugins/platforminputcontexts/qtvirtualkeyboardplugin\.dll$",
    r"(^|/)translations/[^/]+\.qm$",
    r"(^|/)(qml|resources)/",
)]


def _keep(entry) -> bool:
    dest = str(entry[0]).replace("\\", "/")
    return not any(p.search(dest) for p in _DROP)


def _analysis(script: str, hidden: list[str], excludes: list[str]):
    a = Analysis(  # noqa: F821 - defined by PyInstaller
        [str(HERE / script)],
        pathex=[str(ROOT)],
        binaries=[],
        datas=list(METADATA),
        hiddenimports=hidden,
        hookspath=[],
        runtime_hooks=[],
        excludes=excludes,
        noarchive=False,
        optimize=0,
    )
    a.binaries = [e for e in a.binaries if _keep(e)]
    a.datas = [e for e in a.datas if _keep(e)]
    return a


a_gui = _analysis("entry_gui.py", GUI_HIDDEN, EXCLUDES)
a_cli = _analysis("entry_cli.py", CLI_HIDDEN, EXCLUDES + ["PySide6", "shiboken6", "jsonreshape.gui"])

exe_gui = EXE(  # noqa: F821
    PYZ(a_gui.pure),  # noqa: F821
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name="jsonreshape-gui",
    console=False,
    icon=ICON_ARG,
    version=version_resource(ROOT, description="JSON Reshape", internal_name="jsonreshape-gui",
                             original_filename="jsonreshape-gui.exe"),
    debug=False,
    strip=False,
    upx=False,
    disable_windowed_traceback=False,
)
exe_cli = EXE(  # noqa: F821
    PYZ(a_cli.pure),  # noqa: F821
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name="jsonreshape",
    console=True,
    icon=ICON_ARG,
    version=version_resource(ROOT, description="JSON Reshape (command line)", internal_name="jsonreshape",
                             original_filename="jsonreshape.exe"),
    debug=False,
    strip=False,
    upx=False,
)
coll = COLLECT(  # noqa: F821
    exe_gui, a_gui.binaries, a_gui.datas,
    exe_cli, a_cli.binaries, a_cli.datas,
    name="jsonreshape",
    strip=False,
    upx=False,
)
