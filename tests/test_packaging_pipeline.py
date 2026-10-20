"""Static checks of the CI and release pipeline (docs/ARCHITECTURE.md section 16 "Distribution"). They run without a
build, without Rust, Inno Setup or signtool:

* ``.github/workflows/ci.yml`` and ``release.yml`` parse (PyYAML), trigger on the agreed events, pin every action
  (``@v<major>``; ``dtolnay/rust-toolchain@stable`` and ``pypa/gh-action-pypi-publish@release/v1`` are those actions'
  documented forms), have the jobs, markers, environment and timeouts of the design, create the GitHub release as a
  DRAFT and publish to PyPI only behind the ``PYPI_PUBLISH`` variable with trusted publishing;
* ``packaging/installer.iss``: ASCII, the sections, the ``/D`` defines that ``scripts/build_exe.ps1 -Installer``
  passes, the fixed AppId that the winget manifests repeat as ``ProductCode``, 64-bit mode, the PATH code;
* ``scripts/build_exe.ps1``: the ``-Installer`` / ``-Sign`` switches, ISCC and signtool discovery with the explanations
  when they are missing, the signtool arguments, and the script still parses (Windows PowerShell, when present);
* the winget templates fill to valid YAML through ``scripts/winget_manifest.py``;
* ``scripts/release_assets.py``: the version check, ``SHA256SUMS.txt`` on a temporary folder, the CHANGELOG.md
  section, the release notes;
* ``packaging/jsonreshape.spec`` guards its Windows-only parts; the ``dev`` extra has PyYAML; a wheel and an sdist
  built into ``build/wheels`` (``maturin build --release --out build/wheels``; not a test) carry the metadata (skipped
  without them).
"""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import yaml

import jsonreshape

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ISS = ROOT / "packaging" / "installer.iss"
BUILD_SCRIPT = ROOT / "scripts" / "build_exe.ps1"
SPEC = ROOT / "packaging" / "jsonreshape.spec"
WINGET = ROOT / "packaging" / "winget" / "Erkinbek.jsonreshape"
SCRIPTS = ROOT / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import release_assets  # noqa: E402
import winget_manifest  # noqa: E402

PINNED = re.compile(r"^[\w.-]+/[\w.-]+@(v\d+|release/v\d+|stable)$")
GUID = re.compile(r"^AppId=\{\{([0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12})\}$", re.MULTILINE)
SELECTION = "not gui and not bench and not packaging"


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _on(workflow: dict) -> dict:
    return workflow.get("on", workflow.get(True))          # PyYAML reads the key ``on`` as the boolean True


def _steps(job: dict) -> list[dict]:
    return list(job.get("steps", []))


def _runs(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) for step in _steps(job))


def _uses(workflow: dict) -> list[str]:
    return [step["uses"] for job in workflow["jobs"].values() for step in _steps(job) if "uses" in step]


def _iss_text() -> str:
    return ISS.read_text(encoding="utf-8-sig")


def _iss_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "#")):
            continue
        match = re.fullmatch(r"\[(\w+)\]", line)
        if match:
            current = match.group(1)
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return sections


def _app_id() -> str:
    match = GUID.search(_iss_text())
    assert match, "installer.iss: AppId={{<GUID>} (double brace, upper-case GUID)"
    return match.group(1)


# ==================================================================================================
# workflows
# ==================================================================================================
@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_workflow_parses_pins_every_action_and_bounds_every_job(name: str) -> None:
    wf = _workflow(name)
    assert wf["name"] and _on(wf) and wf["jobs"]
    assert wf["permissions"] == {"contents": "read"}, "the default token is read-only; jobs widen it"
    assert "concurrency" in wf
    for job_name, job in wf["jobs"].items():
        assert job.get("runs-on"), job_name
        assert _steps(job), job_name
        assert isinstance(job.get("timeout-minutes"), int) and job["timeout-minutes"] <= 45, job_name
        for step in _steps(job):
            assert ("uses" in step) != ("run" in step), (job_name, step)
    for action in _uses(wf):
        assert PINNED.match(action), f"{name}: action not pinned to a major version: {action}"
    assert {"actions/checkout@v4", "actions/setup-python@v5"} <= set(_uses(wf))


def test_ci_workflow_structure() -> None:
    wf = _workflow("ci.yml")
    assert _on(wf) == {"push": {"branches": ["main"]}, "pull_request": {"branches": ["main"]}}
    assert wf["concurrency"]["cancel-in-progress"] is True and "${{ github.ref }}" in wf["concurrency"]["group"]
    assert wf["env"]["PYTHON_VERSION"] == "3.12" and wf["env"]["QT_QPA_PLATFORM"] == "offscreen"
    assert wf["env"]["PYTEST_SELECTION"] == SELECTION
    jobs = wf["jobs"]
    assert set(jobs) == {"tests-windows", "tests-linux-python-core", "build-exe"}
    assert jobs["build-exe"]["needs"] == "tests-windows"
    assert {"actions/cache@v4", "actions/upload-artifact@v4", "dtolnay/rust-toolchain@stable"} <= set(_uses(wf))
    for name, job in jobs.items():
        assert "JSONRESHAPE_HOME" in job["env"], name
        assert str(job["runs-on"]).startswith("windows" if name != "tests-linux-python-core" else "ubuntu"), name
        keys = [str(step["with"]["key"]) for step in _steps(job) if step.get("uses") == "actions/cache@v4"]
        assert any("hashFiles('pyproject.toml')" in k for k in keys), f"{name}: pip cache keyed on pyproject.toml"
        assert any("hashFiles('rust/Cargo.lock')" in k for k in keys), f"{name}: cargo cache keyed on Cargo.lock"
        python = [step["with"]["python-version"] for step in _steps(job) if step.get("uses") == "actions/setup-python@v5"]
        assert python == ["${{ env.PYTHON_VERSION }}"], name
    for name in ("tests-windows", "tests-linux-python-core"):
        runs = _runs(jobs[name])
        assert "maturin develop --release" in runs and 'pip install -e ".[dev,gui]"' in runs, name
        assert 'pytest -q -p no:cacheprovider -m "${{ env.PYTEST_SELECTION }}" --timeout=600' in runs, name
    assert jobs["tests-linux-python-core"]["env"]["JSONRESHAPE_BACKEND"] == "python"
    assert "core.BACKEND == 'rust'" in _runs(jobs["tests-windows"])
    assert "core.BACKEND == 'python'" in _runs(jobs["tests-linux-python-core"])
    build = _runs(jobs["build-exe"])
    assert "scripts\\build_exe.ps1 -SkipTests -NoInstall" in build and 'pip install -e ".[gui,build]"' in build
    assert "-m packaging tests/test_packaging.py" in build
    upload = [s for s in _steps(jobs["build-exe"]) if s.get("uses") == "actions/upload-artifact@v4"]
    assert len(upload) == 1 and upload[0]["with"]["path"] == "dist/jsonreshape"
    assert upload[0]["with"]["retention-days"] == 14 and upload[0]["with"]["if-no-files-found"] == "error"


def test_release_workflow_structure() -> None:
    wf = _workflow("release.yml")
    assert _on(wf) == {"push": {"tags": ["v*"]}, "workflow_dispatch": None}
    assert wf["concurrency"]["cancel-in-progress"] is False
    jobs = wf["jobs"]
    assert set(jobs) == {"version", "windows-exe", "unix-bundle", "wheels", "sdist", "release", "publish-pypi"}
    assert "release_assets.py check-version --tag" in _runs(jobs["version"])
    assert set(jobs["version"]["outputs"]) == {"version", "tag", "prerelease"}
    # Windows: exe + installer, signed when the secrets exist, zipped
    win = jobs["windows-exe"]
    assert win["needs"] == "version" and str(win["runs-on"]).startswith("windows")
    runs = _runs(win)
    assert "build_exe.ps1 -SkipTests -NoInstall -Installer -Sign $thumbprint" in runs
    assert "build_exe.ps1 -SkipTests -NoInstall -Installer" in runs and "Inno Setup 6\\ISCC.exe" in runs
    assert "-m packaging tests/test_packaging.py" in runs
    assert "Compress-Archive" in runs and "windows-x64.zip" in runs and "-setup-x64.exe" in runs
    cert = [s for s in _steps(win) if s.get("id") == "cert"][0]
    assert set(cert["env"]) == {"PFX_BASE64", "PFX_PASSWORD"} and "secrets.CODE_SIGN_PFX_BASE64" in cert["env"]["PFX_BASE64"]
    # Linux / macOS onedir bundles with a smoke run
    unix = jobs["unix-bundle"]
    assert {e["os"] for e in unix["strategy"]["matrix"]["include"]} == {"ubuntu-22.04", "macos-14"}
    assert {e["name"] for e in unix["strategy"]["matrix"]["include"]} == {"linux-x86_64", "macos-arm64"}
    runs = _runs(unix)
    assert "packaging/jsonreshape.spec" in runs and "JSONRESHAPE_SMOKE_RESULT=" in runs and "--version" in runs
    assert "tar -C dist -czf" in runs and ".tar.gz" in runs
    # wheels: one abi3 wheel per platform + the sdist, with maturin-action
    wheels = jobs["wheels"]
    assert {e["target"] for e in wheels["strategy"]["matrix"]["include"]} == {"x64", "x86_64", "universal2-apple-darwin"}
    assert {e["os"] for e in wheels["strategy"]["matrix"]["include"]} == {"windows-latest", "ubuntu-latest", "macos-14"}
    maturin = [s for s in _steps(wheels) if s.get("uses") == "PyO3/maturin-action@v1"]
    assert len(maturin) == 1 and "--release" in maturin[0]["with"]["args"] and "--out wheels" in maturin[0]["with"]["args"]
    sdist = [s for s in _steps(jobs["sdist"]) if s.get("uses") == "PyO3/maturin-action@v1"]
    assert len(sdist) == 1 and sdist[0]["with"]["command"] == "sdist"
    # the draft release with checksums, notes and the winget manifests
    rel = jobs["release"]
    assert set(rel["needs"]) == {"version", "windows-exe", "unix-bundle", "wheels", "sdist"}
    assert rel["permissions"] == {"contents": "write"}
    runs = _runs(rel)
    assert "release_assets.py sums release" in runs and "release_assets.py notes --version" in runs
    assert "winget_manifest.py --version" in runs and "--installer-url" in runs
    download = [s for s in _steps(rel) if s.get("uses") == "actions/download-artifact@v4"]
    assert download and download[0]["with"]["pattern"] == "release-*" and download[0]["with"]["merge-multiple"] is True
    gh = [s for s in _steps(rel) if s.get("uses") == "softprops/action-gh-release@v2"]
    assert len(gh) == 1 and gh[0]["with"]["draft"] is True, "the author reviews the release before publishing"
    assert gh[0]["with"]["body_path"] == "release_notes.md" and gh[0]["with"]["files"] == "release/*"
    assert gh[0]["with"]["fail_on_unmatched_files"] is True
    uploads = {s["with"]["name"] for job in jobs.values() for s in _steps(job)
               if s.get("uses") == "actions/upload-artifact@v4"}
    assert {"release-windows-x64", "release-${{ matrix.name }}", "release-pypi-wheel-${{ matrix.name }}",
            "release-pypi-sdist", "winget-manifests"} <= uploads
    # PyPI: optional, trusted publishing, tags only
    pypi = jobs["publish-pypi"]
    assert "vars.PYPI_PUBLISH == 'true'" in pypi["if"] and "startsWith(github.ref, 'refs/tags/v')" in pypi["if"]
    assert pypi["permissions"] == {"id-token": "write"} and pypi["environment"]["name"] == "pypi"
    assert "release" in pypi["needs"] and "wheels" in pypi["needs"] and "sdist" in pypi["needs"]
    publish = [s for s in _steps(pypi) if s.get("uses") == "pypa/gh-action-pypi-publish@release/v1"]
    assert len(publish) == 1 and "password" not in publish[0].get("with", {}), "trusted publishing: no token"
    download = [s for s in _steps(pypi) if s.get("uses") == "actions/download-artifact@v4"]
    assert download[0]["with"]["pattern"] == "release-pypi-*"


# ==================================================================================================
# installer script and build script
# ==================================================================================================
def test_installer_script_sections_and_defines() -> None:
    text = _iss_text()
    assert all(ord(ch) < 128 for ch in text), "ISCC reads an .iss without BOM as ANSI: keep it ASCII"
    sections = _iss_sections(text)
    for name in ("Setup", "Languages", "Tasks", "Files", "Icons", "Registry", "Run", "Code"):
        assert sections.get(name), name
    setup = dict(line.split("=", 1) for line in sections["Setup"])
    assert setup["AppVersion"] == "{#AppVersion}" and setup["VersionInfoVersion"] == "{#AppVersionNumeric}"
    assert setup["AppCopyright"] == "{#AppCopyright}" and setup["VersionInfoProductTextVersion"] == "{#AppVersion}"
    assert setup["DefaultDirName"] == "{autopf}\\jsonreshape"
    assert setup["ArchitecturesInstallIn64BitMode"] == "x64compatible" and setup["ArchitecturesAllowed"] == "x64compatible"
    assert setup["OutputDir"] == "{#OutputDir}" and setup["OutputBaseFilename"] == "{#OutputBaseFilename}"
    assert setup["PrivilegesRequired"] == "lowest" and setup["PrivilegesRequiredOverridesAllowed"] == "dialog"
    assert setup["ChangesEnvironment"] == "yes" and setup["LicenseFile"] == "..\\LICENSE"
    assert re.search(r"^#ifndef AppVersion\s*\n\s*#error", text, re.MULTILINE), "no version by hand"
    for define in ("AppVersionNumeric", "AppCopyright", "SourceDir", "OutputDir", "OutputBaseFilename", "IconFile"):
        assert f"#ifndef {define}" in text and f"#define {define}" in text, define
    assert sections["Files"] == ['Source: "{#SourceDir}\\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs']
    assert all("Name:" in line and "Filename:" in line for line in sections["Icons"])
    assert '#define AppExeName "jsonreshape-gui.exe"' in text
    assert any('Filename: "{app}\\{#AppExeName}"' in line for line in sections["Icons"])
    tasks = {line.split('"')[1] for line in sections["Tasks"]}
    assert tasks == {"desktopicon", "addtopath"}
    assert all("Flags: unchecked" in line for line in sections["Tasks"]), "both optional, off by default"
    registry = sections["Registry"]
    assert len(registry) == 1 and "HKCU" in registry[0] and "Tasks: addtopath" in registry[0]
    assert "Check: NeedsAddPath(" in registry[0] and "{olddata};{app}" in registry[0]
    code = "\n".join(sections["Code"])
    assert "function NeedsAddPath(" in code and "procedure RemoveFromPath(" in code
    assert "procedure CurUninstallStepChanged(" in code and "usPostUninstall" in code
    assert "HKEY_CURRENT_USER" in code and "HKEY_LOCAL_MACHINE" not in code, "the user's PATH only"
    _app_id()


def test_build_script_installer_and_sign_additions() -> None:
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert all(ord(ch) < 128 for ch in script)
    assert "[switch]$Installer" in script and '[string]$Sign = ""' in script
    assert ".PARAMETER Installer" in script and ".PARAMETER Sign" in script
    # ISCC: PATH, then the install folder; a clear message when it is missing
    assert "function Find-Iscc" in script and 'Get-Command "ISCC.exe"' in script
    assert 'Join-Path $dir "Inno Setup 6\\ISCC.exe"' in script and "${env:ProgramFiles(x86)}" in script
    assert "winget install JRSoftware.InnoSetup" in script and "https://jrsoftware.org/isdl.php" in script
    assert "The installer was NOT built" in script
    # signtool: PATH, then the Windows SDK; the exact arguments; a clear message when it is missing
    assert "function Find-Signtool" in script and 'Get-Command "signtool.exe"' in script
    assert "Windows Kits\\10\\bin" in script and "Windows SDK Signing Tools for Desktop Apps" in script
    assert ('"sign", "/sha1", $Thumbprint, "/fd", "SHA256", "/tr", "http://timestamp.digicert.com", "/td", "SHA256"'
            in script)
    assert "NOT signed" in script and '"^[0-9A-Fa-f]{40}$"' in script
    # every /D define the script passes is declared (#ifndef) in the .iss, and the version is never typed by hand
    defines = set(re.findall(r'"/D(\w+)=', script))
    assert defines == {"AppVersion", "AppVersionNumeric", "AppCopyright", "SourceDir", "OutputDir",
                       "OutputBaseFilename", "IconFile"}
    iss = _iss_text()
    for define in defines:
        assert f"{{#{define}}}" in iss, define
        assert f"#ifndef {define}" in iss, define
    assert "version_info as v" in script and "v.version_tuple(ver)" in script and "v.COPYRIGHT" in script
    assert '"/Q"' in script and 'Join-Path $Root "packaging\\installer.iss"' in script
    # the executables are signed before the smoke tests, the installer after it is built
    assert script.index("# 4b. code signing") < script.index("# 5. smoke tests") < script.index("# 7. the installer")


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell is needed to parse the script")
def test_build_script_parses_in_windows_powershell() -> None:
    code = ("$tokens = $null; $errors = $null; "
            f"[System.Management.Automation.Language.Parser]::ParseFile('{BUILD_SCRIPT}', [ref]$tokens, [ref]$errors) "
            "| Out-Null; if ($errors.Count) { $errors | ForEach-Object { Write-Output $_.Message }; exit 1 }; "
            "Write-Output 'ok'")
    proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", code], capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0 and proc.stdout.strip().endswith("ok"), proc.stdout + proc.stderr


# ==================================================================================================
# winget
# ==================================================================================================
def test_winget_templates_fill_to_valid_manifests(tmp_path: Path) -> None:
    folder = winget_manifest.template_dir()
    assert folder.parent == WINGET and re.fullmatch(r"\d+(\.\d+)*", folder.name)
    names = sorted(p.name for p in folder.glob("*.yaml"))
    assert names == sorted(winget_manifest.MANIFEST_NAMES)
    for name in names:
        text = (folder / name).read_text(encoding="utf-8")
        assert "{{VERSION}}" in text and "ManifestVersion: 1.6.0" in text, name
    installer_text = (folder / "Erkinbek.jsonreshape.installer.yaml").read_text(encoding="utf-8")
    assert "{{INSTALLER_URL}}" in installer_text and "{{INSTALLER_SHA256}}" in installer_text
    assert "{{RELEASE_DATE}}" in installer_text
    with pytest.raises(KeyError):
        winget_manifest.render("x {{NO_SUCH}} y", {"VERSION": "1"})

    version = jsonreshape.__version__
    installer = tmp_path / f"jsonreshape-{version}-setup-x64.exe"
    installer.write_bytes(b"MZ not really an installer " * 1000)
    url = f"https://github.com/Erkinbek/jsonreshape/releases/download/v{version}/{installer.name}"
    out = tmp_path / "out"
    assert winget_manifest.main(["--version", version, "--installer-url", url, "--installer", str(installer),
                                 "--release-date", "2026-10-20", "--out", str(out)]) == 0
    target = out / "manifests" / "e" / "Erkinbek" / "jsonreshape" / version
    assert target == winget_manifest.output_dir(out, version)
    written = sorted(p.name for p in target.iterdir())
    assert written == names
    docs = {}
    for name in written:
        text = (target / name).read_text(encoding="utf-8")
        assert "{{" not in text and "TEMPLATE" not in text, name
        assert text.startswith("# yaml-language-server: $schema=https://aka.ms/winget-manifest."), name
        docs[name] = yaml.safe_load(text)
    kinds = {d["ManifestType"] for d in docs.values()}
    assert kinds == {"version", "installer", "defaultLocale"}
    for doc in docs.values():
        assert doc["PackageIdentifier"] == "Erkinbek.jsonreshape" and str(doc["PackageVersion"]) == version
        assert doc["ManifestVersion"] == "1.6.0"
    inst = docs["Erkinbek.jsonreshape.installer.yaml"]
    assert inst["InstallerType"] == "inno" and inst["ReleaseDate"] == dt.date(2026, 10, 20)
    assert {i["Scope"] for i in inst["Installers"]} == {"user", "machine"}
    digest = hashlib.sha256(installer.read_bytes()).hexdigest().upper()
    for entry in inst["Installers"]:
        assert entry["Architecture"] == "x64" and entry["InstallerUrl"] == url and entry["InstallerSha256"] == digest
        assert entry["ProductCode"] == "{" + _app_id() + "}_is1", "winget upgrades find the Inno Setup uninstall key"
        assert "/VERYSILENT" in entry["InstallerSwitches"]["Silent"]
        assert ("/ALLUSERS" if entry["Scope"] == "machine" else "/CURRENTUSER") in entry["InstallerSwitches"]["Silent"]
    assert "jsonreshape" in inst["Commands"] and {"json", "jsonl"} <= set(inst["FileExtensions"])
    locale = docs["Erkinbek.jsonreshape.locale.en-US.yaml"]
    assert locale["PackageLocale"] == "en-US" and locale["License"] == "Apache-2.0"
    assert locale["Publisher"] == "Erkin Pardayev" and locale["PackageName"] == "JSON Reshape"
    assert locale["ReleaseNotesUrl"].endswith(f"/releases/tag/v{version}")
    assert docs["Erkinbek.jsonreshape.yaml"]["DefaultLocale"] == "en-US"
    # --sha256 instead of a file; a wrong digest is refused
    assert winget_manifest.main(["--version", version, "--installer-url", url, "--sha256", digest.lower(),
                                 "--out", str(tmp_path / "out2")]) == 0
    assert winget_manifest.main(["--version", version, "--installer-url", url, "--sha256", "abc"]) == 2
    assert winget_manifest.main(["--version", version, "--installer-url", url]) == 2


# ==================================================================================================
# release_assets.py
# ==================================================================================================
def test_release_assets_versions(capsys: pytest.CaptureFixture[str]) -> None:
    version = jsonreshape.__version__
    assert release_assets.read_version(ROOT) == version
    assert release_assets.pyproject_version(ROOT) == version and release_assets.cargo_version(ROOT) == version
    assert release_assets.check_versions(ROOT, "v" + version) == []
    problems = release_assets.check_versions(ROOT, "v9.9.9")
    assert len(problems) == 3 and all("9.9.9" in p for p in problems)
    assert release_assets.check_versions(ROOT, version)[0].startswith("the tag must be v<version>")
    assert release_assets.main(["version"]) == 0 and capsys.readouterr().out.strip() == version
    assert release_assets.main(["check-version", "--tag", "v" + version]) == 0
    assert capsys.readouterr().out.startswith("ok:")
    assert release_assets.main(["check-version", "--tag", "v9.9.9"]) == 1
    assert "error:" in capsys.readouterr().err


def test_release_assets_sums(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "b.zip").write_bytes(b"zip" * 5000)
    (tmp_path / "a.whl").write_bytes(b"\x00\x01" * 3000)
    (tmp_path / "sub").mkdir()
    out = release_assets.write_sums(tmp_path)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert out.name == "SHA256SUMS.txt" and [ln.split("  ")[1] for ln in lines] == ["a.whl", "b.zip"]
    for line in lines:
        digest, name = line.split("  ")
        assert digest == hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
    assert release_assets.write_sums(tmp_path) == out and len(out.read_text(encoding="utf-8").splitlines()) == 2
    assert release_assets.read_sums(out) == {n: d for d, n in (ln.split("  ") for ln in lines)}
    assert release_assets.main(["sums", str(tmp_path)]) == 0 and "2 files" in capsys.readouterr().out
    with pytest.raises(FileNotFoundError):
        release_assets.write_sums(tmp_path / "sub")


SAMPLE_CHANGELOG = """# Changelog

## [Unreleased]

### Added
- something new

## [0.2.0] — 2026-11-01

### Fixed
- a bug

## [0.1.0] — 2026-10-20

The first version.

[Unreleased]: https://example.com/compare/v0.2.0...HEAD
[0.2.0]: https://example.com/releases/tag/v0.2.0
[0.1.0]: https://example.com/releases/tag/v0.1.0
"""


def test_changelog_section() -> None:
    body, found = release_assets.changelog_section(SAMPLE_CHANGELOG, "0.2.0")
    assert found and body == "### Fixed\n- a bug"
    body, found = release_assets.changelog_section(SAMPLE_CHANGELOG, "0.1.0")
    assert found and body == "The first version." and "example.com" not in body
    body, found = release_assets.changelog_section(SAMPLE_CHANGELOG, "0.3.0")
    assert not found and body == "### Added\n- something new"
    assert release_assets.changelog_section("nothing here", "1.0") == ("", False)
    real = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    body, found = release_assets.changelog_section(real, "0.1.0")
    assert found and "The first complete version" in body and "releases/tag/v0.1.0" not in body
    assert "## [" not in body
    unreleased, found = release_assets.changelog_section(real, "999.0")
    assert not found and unreleased


def test_release_notes(tmp_path: Path) -> None:
    version = "0.2.0"
    names = [f"jsonreshape-{version}-setup-x64.exe", f"jsonreshape-{version}-windows-x64.zip",
             f"jsonreshape-{version}-linux-x86_64.tar.gz", f"jsonreshape-{version}-macos-arm64.tar.gz",
             f"jsonreshape-{version}-cp311-abi3-win_amd64.whl", f"jsonreshape-{version}.tar.gz", "SHA256SUMS.txt"]
    for i, name in enumerate(names):
        (tmp_path / name).write_bytes(bytes([i]) * (1500 * (i + 1)))
    notes = release_assets.release_notes(version, SAMPLE_CHANGELOG, sorted(tmp_path.iterdir()), repo="o/r")
    assert notes.startswith(f"# jsonreshape {version}\n\n### Fixed\n- a bug\n")
    assert "TODO" not in notes
    for name in names[:-1]:
        assert f"`{name}`" in notes
        digest = hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
        assert f"| `{name}` | " in notes and digest in notes
    assert "| `SHA256SUMS.txt` |" not in notes and "`sha256sum -c SHA256SUMS.txt" in notes
    assert "pip install jsonreshape" in notes and "after the first publish" in notes
    assert "winget install Erkinbek.jsonreshape" in notes and "https://github.com/o/r/tree/v0.2.0" in notes
    assert notes.index("**Windows installer**") < notes.index("**Windows, portable**") < notes.index("**Linux")
    # a version the changelog does not know yet: the Unreleased text with a TODO; no assets: no table
    notes = release_assets.release_notes("0.3.0", SAMPLE_CHANGELOG)
    assert "TODO" in notes and "- something new" in notes and "## Assets" not in notes
    out = tmp_path / "notes.md"
    assert release_assets.main(["notes", "--version", version, "--changelog", str(tmp_path / "none.md"),
                                "--assets", str(tmp_path), "--out", str(out)]) == 0
    assert out.read_text(encoding="utf-8").startswith(f"# jsonreshape {version}\n\n> **TODO:**")


# ==================================================================================================
# spec, pyproject, built metadata
# ==================================================================================================
def test_spec_guards_its_windows_only_parts() -> None:
    text = SPEC.read_text(encoding="utf-8")
    ast.parse(text, filename=str(SPEC))
    assert text.count('sys.platform == "win32"') >= 3, "icon, version resource, Qt drop list"
    assert 'ICON_ARG = [str(ICON)] if sys.platform == "win32" and ICON.is_file() else None' in text
    assert text.count("version=_version(") == 2 and text.count("version_resource(ROOT") == 1
    assert 'return version_resource(ROOT, **kwargs) if sys.platform == "win32" else None' in text
    assert "libQt6Network" in text and r"so(\.\d+)*" in text and r"\.framework/" in text
    assert '"|opengl|dbus" if sys.platform == "win32" else ""' in text, "QtDBus / QtOpenGL stay on Linux"


def test_dev_extra_has_pyyaml_and_the_sdist_carries_the_license() -> None:
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "pyyaml>=6" in data["project"]["optional-dependencies"]["dev"]
    include = data["tool"]["maturin"]["include"]
    assert {"LICENSE", "NOTICE", "CHANGELOG.md"} <= {e["path"] for e in include}
    assert all(e["format"] == "sdist" for e in include)


def test_built_wheel_and_sdist_metadata() -> None:
    """``maturin build --release --out build/wheels`` + ``maturin sdist --out build/wheels`` (a developer step, not a
    test): their metadata, when they exist."""
    wheels = sorted((ROOT / "build" / "wheels").glob("jsonreshape-*.whl")) if (ROOT / "build" / "wheels").is_dir() else []
    if not wheels:
        pytest.skip("no wheel in build/wheels (maturin build --release --out build/wheels)")
    version = jsonreshape.__version__
    with zipfile.ZipFile(wheels[-1]) as z:
        names = z.namelist()
        meta = z.read(f"jsonreshape-{version}.dist-info/METADATA").decode("utf-8").split("\n\n", 1)[0]
        wheel = z.read(f"jsonreshape-{version}.dist-info/WHEEL").decode("utf-8")
    fields = {}
    for line in meta.splitlines():
        key, _, value = line.partition(": ")
        fields.setdefault(key, []).append(value)
    assert fields["Name"] == ["jsonreshape"] and fields["Version"] == [version]
    assert fields["License"] == ["Apache-2.0"] and fields["Requires-Python"] == [">=3.11"]
    assert "License :: OSI Approved :: Apache Software License" in fields["Classifier"]
    assert any(c.startswith("Programming Language :: Python :: 3.1") for c in fields["Classifier"])
    assert {"LICENSE", "NOTICE"} <= set(fields["License-File"])
    assert any(v.startswith("orjson") for v in fields["Requires-Dist"])
    assert "Tag: cp311-abi3-" in wheel, "one abi3 wheel per platform (pyo3 abi3-py311)"
    assert "jsonreshape/core/_pycore.py" in names and "jsonreshape/core/_rscore.pyi" in names
    assert any(n.startswith("jsonreshape/core/_rscore.") and n.endswith((".pyd", ".so")) for n in names)
    assert not any(n.endswith(".pdb") for n in names)
    assert f"jsonreshape-{version}.dist-info/licenses/LICENSE" in names
    sdists = sorted((ROOT / "build" / "wheels").glob(f"jsonreshape-{version}.tar.gz"))
    if sdists:
        with tarfile.open(sdists[-1]) as t:
            members = t.getnames()
        top = {n.split("/", 1)[1].split("/", 1)[0] for n in members if "/" in n}
        assert {"LICENSE", "NOTICE", "CHANGELOG.md", "README.md", "pyproject.toml", "jsonreshape", "rust"} <= top
        assert not any(n.endswith((".pyd", ".pdb", ".so")) for n in members)
