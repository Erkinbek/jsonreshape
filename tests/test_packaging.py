"""The PyInstaller build (docs/ARCHITECTURE.md §15.0 ".exe"; AGENT_PROMPT §2).

Tests marked ``packaging`` run against ``dist/jsonreshape`` -- built by ``scripts/build_exe.ps1``, which runs them --
and are skipped while it does not exist: the layout (two executables sharing one ``_internal``, the Rust core,
the Qt plugins; nothing that is excluded), the version resources and subsystems, every DLL finds its imports,
``--version``, ``inspect``, ``convert`` (byte-identical to the CLI run from source), worker processes of the frozen
CLI (``multiprocessing.freeze_support``), the GUI exe started offscreen with a file and closed by its smoke hook
(also with a missing file), and the start times (written to ``$JSONRESHAPE_PACKAGING_TIMES`` for the build script).

The unmarked tests check the build inputs without a build: the version-resource helper, the icon file, the pinned
PyInstaller (build script = pyproject) and the GUI smoke hook run from source.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

import jsonreshape

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "jsonreshape"
CLI = DIST / "jsonreshape.exe"
GUI = DIST / "jsonreshape-gui.exe"
INTERNAL = DIST / "_internal"
PACKAGING = ROOT / "packaging"

built = pytest.mark.skipif(not (CLI.is_file() and GUI.is_file()),
                           reason="no build in dist/jsonreshape (scripts/build_exe.ps1 builds it)")
#: The build script sets it: where ``test_start_times`` writes the measured times (tests write nothing else
#: outside their temporary folders).
TIMES_ENV = "JSONRESHAPE_PACKAGING_TIMES"

DATA = ('[{"id": 0, "name": "O\'zbekiston", "city": "Тошкент", "n": 0.1, "big": 12345678901234567890, '
        '"neg": -0.0, "x": 1.10, "huge": 1e400, "esc": "a\\"b\\\\c\\u00e9"},\n'
        ' {"id": 1, "name": "G\'ijduvon", "city": "Buxoro", "n": 2.50, "tags": ["a", "b"], "big": null}]\n')


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "JSONRESHAPE_", "QT_"))}
    env.update(JSONRESHAPE_HOME=str(tmp_path / "home"), JSONRESHAPE_LANG="uz", QT_QPA_PLATFORM="offscreen",
               PYTHONIOENCODING="utf-8")
    env.update(extra)
    return env


def _run(argv: list, tmp_path: Path, *, timeout: float = 180, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in argv], env=_env(tmp_path, **extra), capture_output=True,
                          timeout=timeout, cwd=str(tmp_path))


def _data(tmp_path: Path) -> Path:
    p = tmp_path / "ma'lumot ўзбек.json"
    p.write_text(DATA, encoding="utf-8")
    return p


def _gui_smoke(exe: list, tmp_path: Path, files: list[Path], name: str = "smoke.json",
               timeout_ms: int = 60000) -> tuple[subprocess.CompletedProcess, dict, float]:
    result = tmp_path / name
    started = time.perf_counter()
    proc = _run([*exe, *files], tmp_path, timeout=120, JSONRESHAPE_SMOKE_RESULT=str(result),
                JSONRESHAPE_SMOKE_TIMEOUT_MS=str(timeout_ms))
    wall = (time.perf_counter() - started) * 1000.0
    report = json.loads(result.read_text(encoding="utf-8")) if result.is_file() else {}
    return proc, report, wall


# ==================================================================================================
# without a build
# ==================================================================================================
def _version_info():
    sys.path.insert(0, str(PACKAGING))
    try:
        import version_info
    finally:
        sys.path.remove(str(PACKAGING))
    return version_info


def test_version_info_helper() -> None:
    vi = _version_info()
    assert vi.app_version(ROOT) == jsonreshape.__version__
    assert vi.version_tuple("0.1.0") == (0, 1, 0, 0)
    assert vi.version_tuple("1.2.3.4.5") == (1, 2, 3, 4)
    assert vi.version_tuple("2.0rc1") == (2, 0, 0, 0)
    assert vi.version_tuple("v3") == (3, 0, 0, 0)
    for bad in ("x", "", "1.70000"):
        with pytest.raises(ValueError):
            vi.version_tuple(bad)
    strings = vi.version_strings(ROOT, description="JSON Reshape", internal_name="jsonreshape-gui",
                                 original_filename="jsonreshape-gui.exe")
    assert strings["ProductVersion"] == strings["FileVersion"] == jsonreshape.__version__
    assert strings["ProductName"] == "JSON Reshape" and strings["OriginalFilename"] == "jsonreshape-gui.exe"
    pytest.importorskip("PyInstaller")
    res = vi.version_resource(ROOT, description="JSON Reshape", internal_name="jsonreshape-gui",
                              original_filename="jsonreshape-gui.exe")
    assert "JSON Reshape" in str(res) and jsonreshape.__version__ in str(res)


def test_the_pinned_pyinstaller_is_the_same_everywhere() -> None:
    script = (ROOT / "scripts" / "build_exe.ps1").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    pins = {name: re.search(rf'\${name}\s*=\s*"([^"]+)"', script).group(1)
            for name in ("PyInstallerVersion", "HooksContribVersion")}
    assert f'"pyinstaller=={pins["PyInstallerVersion"]}"' in pyproject
    assert f'"pyinstaller-hooks-contrib=={pins["HooksContribVersion"]}"' in pyproject
    assert all(ord(ch) < 128 for ch in script), "Windows PowerShell 5.1 reads a script without BOM as ANSI"
    assert "packaging" in pyproject and re.search(r'"packaging:', pyproject)


@pytest.mark.gui
def test_make_icon_writes_a_multi_size_ico(qapp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QT_QPA_FONTDIR", os.environ.get("QT_QPA_FONTDIR", ""))     # render() may set it
    sys.path.insert(0, str(PACKAGING))
    try:
        import make_icon
    finally:
        sys.path.remove(str(PACKAGING))
    images = make_icon.render()
    data = make_icon.ico_bytes(images)
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind, count) == (0, 1, len(make_icon.SIZES))
    for i, size in enumerate(make_icon.SIZES):
        w, h, _c, _r, planes, bits, length, offset = struct.unpack("<BBBBHHII", data[6 + 16 * i:22 + 16 * i])
        assert (w or 256) == (h or 256) == size and planes == 1 and bits == 32
        assert data[offset:offset + 8] == b"\x89PNG\r\n\x1a\n" and offset + length <= len(data)
    out = tmp_path / "icon.ico"
    assert make_icon.main([str(out)]) == 0 and out.read_bytes()[:6] == data[:6]


def test_the_gui_smoke_hook_from_source(tmp_path: Path) -> None:
    """The hook of ``packaging/entry_gui.py`` works before any build (here with Python)."""
    data = _data(tmp_path)
    proc, report, _wall = _gui_smoke([sys.executable, str(PACKAGING / "entry_gui.py")], tmp_path, [data])
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert report["ok"] and not report["frozen"] and report["platform"] == "offscreen"
    assert report["tabs"] == [{"name": data.name, "state": "ready", "records": 2}]
    assert report["network_modules"] == [] and report["version"] == jsonreshape.__version__
    assert 0 < report["times_ms"]["window"] <= report["times_ms"]["ready"]
    if sys.platform == "win32" and Path(sys.executable).with_name("pythonw.exe").is_file():
        assert report["open_with_command"].endswith(' -m jsonreshape.gui "%1"')


# ==================================================================================================
# against the build
# ==================================================================================================
_ABSENT = re.compile(
    r"(^|/)(qt6(network|qml|quick|webengine|3d|multimedia|pdf|virtualkeyboard|opengl)\w*\.dll|opengl32sw\.dll"
    r"|_tkinter\.pyd|tcl\d*t?\.dll|tk\d*t?\.dll|_ssl\.pyd|libssl[-\w]*\.dll|[^/]+\.qm)$|(^|/)(tests?|pytest|_pytest"
    r"|hypothesis|PyInstaller|tkinter|qml)/", re.IGNORECASE)


@pytest.mark.packaging
@built
def test_layout_two_executables_one_shared_folder() -> None:
    assert sorted(p.name for p in DIST.iterdir()) == ["_internal", "jsonreshape-gui.exe", "jsonreshape.exe"]
    assert (INTERNAL / "jsonreshape" / "core" / "_rscore.pyd").is_file()            # the Rust core
    plugins = INTERNAL / "PySide6" / "plugins"
    for rel in ("platforms/qwindows.dll", "platforms/qoffscreen.dll", "imageformats/qjpeg.dll",
                "imageformats/qgif.dll", "imageformats/qwebp.dll", "imageformats/qico.dll",
                "iconengines/qsvgicon.dll"):
        assert (plugins / rel).is_file(), rel
    assert any((plugins / "styles").iterdir())
    files = [p.relative_to(DIST).as_posix() for p in DIST.rglob("*") if p.is_file()]
    shipped_wrongly = [f for f in files if _ABSENT.search(f)]
    assert not shipped_wrongly, shipped_wrongly
    assert not [f for f in files if f.lower().endswith(".pyd") and "/jsonreshape/" in f and "_rscore" not in f]


def _version_strings(path: Path) -> dict[str, object]:
    import ctypes
    from ctypes import wintypes

    ver = ctypes.WinDLL("version")
    ver.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    ver.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    ver.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                   ctypes.POINTER(wintypes.UINT)]
    size = ver.GetFileVersionInfoSizeW(str(path), None)
    assert size, f"no version resource in {path}"
    buf = ctypes.create_string_buffer(size)
    assert ver.GetFileVersionInfoW(str(path), 0, size, buf)
    out: dict[str, object] = {}
    ptr, length = ctypes.c_void_p(), wintypes.UINT()
    assert ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length))
    fixed = ctypes.string_at(ptr.value, length.value)
    ms, ls = struct.unpack_from("<II", fixed, 8)
    out["fixed"] = (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    for name in ("FileDescription", "ProductName", "ProductVersion", "FileVersion", "OriginalFilename"):
        if ver.VerQueryValueW(buf, f"\\StringFileInfo\\040904B0\\{name}", ctypes.byref(ptr), ctypes.byref(length)):
            out[name] = ctypes.wstring_at(ptr.value, max(0, length.value - 1))
    return out


def _subsystem(path: Path) -> int:
    data = path.read_bytes()[:4096]
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    assert data[pe:pe + 4] == b"PE\0\0"
    return struct.unpack_from("<H", data, pe + 24 + 68)[0]       # optional header: Subsystem


@pytest.mark.packaging
@built
@pytest.mark.skipif(sys.platform != "win32", reason="Windows executables")
def test_version_resources_and_subsystems() -> None:
    vi = _version_info()
    numbers = vi.version_tuple(jsonreshape.__version__)
    for exe, description in ((GUI, "JSON Reshape"), (CLI, "JSON Reshape (command line)")):
        info = _version_strings(exe)
        assert info["fixed"] == numbers
        assert info["ProductVersion"] == info["FileVersion"] == jsonreshape.__version__
        assert info["FileDescription"] == description and info["ProductName"] == "JSON Reshape"
        assert info["OriginalFilename"] == exe.name
    assert _subsystem(GUI) == 2          # IMAGE_SUBSYSTEM_WINDOWS_GUI: no console window
    assert _subsystem(CLI) == 3          # IMAGE_SUBSYSTEM_WINDOWS_CUI


@pytest.mark.packaging
@built
@pytest.mark.skipif(sys.platform != "win32", reason="Windows executables")
def test_every_binary_finds_its_imports() -> None:
    """No DLL the build left out is needed by one it kept (e.g. a Qt plugin linking QtNetwork)."""
    pefile = pytest.importorskip("pefile")
    shipped = {p.name.lower() for p in DIST.rglob("*") if p.suffix.lower() in (".dll", ".pyd", ".exe")}
    system = Path(os.environ.get("SystemRoot", "C:\\Windows")) / "System32"
    missing = []
    for path in DIST.rglob("*"):
        if path.suffix.lower() not in (".dll", ".pyd", ".exe"):
            continue
        pe = pefile.PE(str(path), fast_load=True)
        try:
            pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]])
            for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
                name = entry.dll.decode("ascii", "replace").lower()
                if name in shipped or name.startswith(("api-ms-win-", "ext-ms-")) or (system / name).is_file():
                    continue
                missing.append(f"{path.relative_to(DIST).as_posix()} -> {name}")
        finally:
            pe.close()
    assert not missing, "\n".join(missing)


@pytest.mark.packaging
@built
def test_cli_version(tmp_path: Path) -> None:
    proc = _run([CLI, "--version"], tmp_path)
    out = proc.stdout.decode("utf-8")
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert out.splitlines()[0] == f"jsonreshape {jsonreshape.__version__}"
    assert "Rust" in out and "Python: 3." in out


@pytest.mark.packaging
@built
def test_cli_inspect(tmp_path: Path) -> None:
    data = _data(tmp_path)
    proc = _run([CLI, "inspect", data, "--json"], tmp_path)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    info = json.loads(proc.stdout.decode("utf-8"))
    assert info["records"] == 2 and info["format"]["kind"] == "array" and info["core_backend"] == "rust"
    proc = _run([CLI, "inspect", data], tmp_path)
    text = proc.stdout.decode("utf-8")
    assert proc.returncode == 0 and "Yozuvlar soni: 2" in text and "O'zbekiston" in text


@pytest.mark.packaging
@built
@pytest.mark.parametrize("target, extra", [("out.csv", []), ("out.jsonl", []), ("out.json", ["--indent", "2"]),
                                           ("out.xlsx", []), ("sorted.json", ["--sort-by", "-id", "--compact"])])
def test_cli_convert_equals_the_source_cli(tmp_path: Path, target: str, extra: list[str]) -> None:
    data = _data(tmp_path)
    frozen_out, source_out = tmp_path / "frozen" / target, tmp_path / "source" / target
    frozen_out.parent.mkdir()
    source_out.parent.mkdir()
    proc = _run([CLI, "convert", data, frozen_out, *extra], tmp_path)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    ref = _run([sys.executable, "-m", "jsonreshape", "convert", data, source_out, *extra], tmp_path,
               PYTHONPATH=str(ROOT))
    assert ref.returncode == 0, ref.stderr.decode("utf-8", "replace")
    if target.endswith(".xlsx"):
        import zipfile

        with zipfile.ZipFile(frozen_out) as a, zipfile.ZipFile(source_out) as b:     # (docProps: the time)
            names = [n for n in a.namelist() if n.startswith(("xl/worksheets/", "xl/sharedStrings"))]
            assert names and sorted(names) == sorted(n for n in b.namelist() if n in names)
            assert all(a.read(n) == b.read(n) for n in names)
    else:
        assert frozen_out.read_bytes() == source_out.read_bytes()
    if target == "out.json":
        assert b"12345678901234567890" in frozen_out.read_bytes() and b"1.10" in frozen_out.read_bytes()


@pytest.mark.packaging
@built
def test_cli_worker_processes(tmp_path: Path) -> None:
    """``apply --workers 2`` starts worker processes of the frozen exe (``freeze_support``): same output as one."""
    from jsonreshape.mapping import Designer, infer_tree, make_input_info, save_mapping
    from jsonreshape.reader import Document

    src = tmp_path / "people.json"
    rows = [{"id": i, "name": f"ism {i}", "contact": {"city": ["Toshkent", "Xiva"][i % 2], "phone": f"+998 {i}"},
             "score": i / 10} for i in range(3000)]
    src.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    doc = Document.open(src)
    try:
        doc.build_index()
        vals = [doc.record(i) for i in range(doc.record_count)]
        designer = Designer(infer_tree(vals, root_kind="records"), population=lambda: iter(vals), undo_limit=50)
        tree = designer.tree
        assert designer.move(tree.find("[].contact.city"), tree.find("[].name"), "before").applied
        assert designer.rename(tree.find("[].score"), "ball").applied
        mapping = tmp_path / "map.json"
        save_mapping(designer.tree, mapping, input_info=make_input_info(doc))
    finally:
        doc.close()
    one, two = tmp_path / "one.json", tmp_path / "two.json"
    p1 = _run([CLI, "apply", mapping, src, one, "--workers", "1", "--lang", "en"], tmp_path)
    p2 = _run([CLI, "apply", mapping, src, two, "--workers", "2", "--lang", "en"], tmp_path)
    assert p1.returncode == 0 and p2.returncode == 0, p2.stderr.decode("utf-8", "replace")
    assert b"1 process)" in p1.stderr and b"2 processes)" in p2.stderr, p2.stderr
    assert one.read_bytes() == two.read_bytes()
    first = json.loads(one.read_text(encoding="utf-8"))[0]
    assert list(first).index("city") < list(first).index("name") and first["ball"] == 0 and "score" not in first


@pytest.mark.packaging
@built
def test_gui_starts_offscreen_with_a_file_and_closes(tmp_path: Path) -> None:
    data = _data(tmp_path)
    proc, report, _wall = _gui_smoke([GUI], tmp_path, [data])
    assert proc.returncode == 0, (proc.stderr.decode("utf-8", "replace"), report)
    assert report["ok"] and report["frozen"] and report["platform"] == "offscreen"
    assert Path(report["executable"]).resolve() == GUI.resolve()
    assert report["tabs"] == [{"name": data.name, "state": "ready", "records": 2}]
    assert report["version"] == jsonreshape.__version__
    assert report["core_backend"] == "rust" and report["ijson_backend"] == "yajl2_c"
    assert report["network_modules"] == []
    assert {"png", "jpeg", "gif", "webp", "ico", "svg"} <= set(report["image_formats"])
    assert "pdf" not in report["image_formats"]                # the PDF plugin is not shipped
    assert "Fusion" in report["styles"]
    assert 0 < report["times_ms"]["window"] <= report["times_ms"]["ready"] < 30000
    # the packaged exe registers itself for "Open with" (Settings ▸ General; nothing is written by this test)
    assert report["open_with_command"] == f'"{report["executable"]}" "%1"'


@pytest.mark.packaging
@built
def test_gui_reports_a_file_that_does_not_open(tmp_path: Path) -> None:
    missing = tmp_path / "yo'q.json"
    proc, report, _wall = _gui_smoke([GUI], tmp_path, [missing])
    assert proc.returncode == 3 and report["ok"] is False and not report["timed_out"]
    assert [t["state"] for t in report["tabs"]] == ["failed"]


@pytest.mark.packaging
@built
def test_start_times(tmp_path: Path) -> None:
    """Median of 3 runs each (written to ``$JSONRESHAPE_PACKAGING_TIMES`` when set: the build script prints them)."""
    data = _data(tmp_path)
    cli_ms = []
    for _ in range(3):
        started = time.perf_counter()
        assert _run([CLI, "--version"], tmp_path).returncode == 0
        cli_ms.append((time.perf_counter() - started) * 1000.0)
    window, ready, total = [], [], []
    for i in range(3):
        proc, report, wall = _gui_smoke([GUI], tmp_path, [data], name=f"run{i}.json")
        assert proc.returncode == 0 and report["ok"]
        window.append(report["times_ms"]["window"])
        ready.append(report["times_ms"]["ready"])
        total.append(wall)
    times = {"runs": 3, "cli_version_ms": round(statistics.median(cli_ms)),
             "gui_window_ms": round(statistics.median(window)), "gui_ready_ms": round(statistics.median(ready)),
             "gui_total_ms": round(statistics.median(total)), "measured": time.strftime("%Y-%m-%d %H:%M:%S")}
    target = os.environ.get(TIMES_ENV)
    if target:
        Path(target).write_text(json.dumps(times, indent=1), encoding="utf-8")
    assert times["gui_ready_ms"] < 15000 and times["cli_version_ms"] < 15000, times
