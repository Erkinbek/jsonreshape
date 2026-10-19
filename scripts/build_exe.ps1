<#
.SYNOPSIS
    Builds dist\jsonreshape\ (jsonreshape.exe + jsonreshape-gui.exe, one shared _internal folder) with PyInstaller
    and runs the packaging smoke tests.

.DESCRIPTION
    Developer step (docs/ARCHITECTURE.md section 15.0 ".exe"). Run it from anywhere:

        powershell -ExecutionPolicy Bypass -File scripts\build_exe.ps1

    1. the project's Python (.venv\Scripts\python.exe, or -Python);
    2. PyInstaller: installed into that environment when it is missing or another version (the pinned versions
       below = pyproject.toml [project.optional-dependencies].build). This pip install is the only network
       access, and only here: the program itself is offline;
    3. the icon: packaging\make_icon.py -> build\jsonreshape.ico;
    4. PyInstaller: packaging\jsonreshape.spec -> dist\jsonreshape\ (work files in build\pyinstaller);
    5. the smoke tests (tests\test_packaging.py, marker "packaging": --version, inspect, convert, worker
       processes, the GUI exe started offscreen with a file and closed by its smoke hook, start times), unless
       -SkipTests;
    6. the folder's size, file count and the measured start times.

    build\ and dist\ are not committed (.gitignore). Close running copies of the program first (Windows keeps
    the files of a running exe locked).

.PARAMETER Python
    The Python interpreter to build with (default: .venv\Scripts\python.exe of the project).

.PARAMETER SkipTests
    Build only.

.PARAMETER NoInstall
    Fail instead of installing PyInstaller.
#>
[CmdletBinding()]
param(
    [string]$Python = "",
    [switch]$SkipTests,
    [switch]$NoInstall
)

$ErrorActionPreference = "Stop"
$PyInstallerVersion = "6.22.3"
$HooksContribVersion = "2026.8"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $Python) { $Python = Join-Path $Root ".venv\Scripts\python.exe" }
if (-not (Test-Path $Python)) { throw "Python not found: $Python (create the venv first, see README)" }
$Dist = Join-Path $Root "dist\jsonreshape"
$Build = Join-Path $Root "build"

function Invoke-Checked {
    param([string]$What, [string[]]$Arguments)
    Write-Host "==> $What"
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
}

Push-Location $Root
try {
    # 2. the pinned PyInstaller (Windows PowerShell 5.1 turns a native command's stderr into an error under
    # "Stop": the probe runs under "Continue")
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $installed = & $Python -c "import PyInstaller; print(PyInstaller.__version__)" 2>$null
        if ($LASTEXITCODE -ne 0) { $installed = "" }
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if ($installed -ne $PyInstallerVersion) {
        if ($NoInstall) { throw "PyInstaller $PyInstallerVersion is not installed (found: '$installed')" }
        Invoke-Checked "pip install pyinstaller==$PyInstallerVersion (developer step: network access)" @(
            "-m", "pip", "install", "--disable-pip-version-check",
            "pyinstaller==$PyInstallerVersion", "pyinstaller-hooks-contrib==$HooksContribVersion")
    }

    # 3. the icon
    New-Item -ItemType Directory -Force -Path $Build | Out-Null
    Invoke-Checked "icon -> build\jsonreshape.ico" @("packaging\make_icon.py", (Join-Path $Build "jsonreshape.ico"))

    # 4. PyInstaller
    $started = Get-Date
    Invoke-Checked "PyInstaller (packaging\jsonreshape.spec)" @(
        "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
        "--distpath", (Join-Path $Root "dist"), "--workpath", (Join-Path $Build "pyinstaller"),
        "packaging\jsonreshape.spec")
    $seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
    Write-Host "    built in $seconds s"

    # 5. smoke tests (test_start_times writes the measured times into $timesFile)
    $timesFile = Join-Path $Build "packaging_smoke.json"
    if (Test-Path $timesFile) { Remove-Item -Force $timesFile }
    if (-not $SkipTests) {
        $env:JSONRESHAPE_PACKAGING_TIMES = $timesFile
        try {
            Invoke-Checked "smoke tests (tests\test_packaging.py -m packaging)" @(
                "-m", "pytest", "-q", "-p", "no:cacheprovider", "-m", "packaging", "tests\test_packaging.py")
        }
        finally {
            Remove-Item Env:\JSONRESHAPE_PACKAGING_TIMES -ErrorAction SilentlyContinue
        }
    }

    # 6. size and start times
    $files = Get-ChildItem -Path $Dist -Recurse -File
    $bytes = ($files | Measure-Object -Property Length -Sum).Sum
    $mb = [math]::Round($bytes / 1MB, 1)
    Write-Host ""
    Write-Host ("dist\jsonreshape: {0} MB ({1:N0} bytes) in {2} files" -f $mb, $bytes, $files.Count)
    if (Test-Path $timesFile) {
        $times = Get-Content -Raw -Encoding UTF8 $timesFile | ConvertFrom-Json
        $line = "start times (median of {0} runs): jsonreshape.exe --version {1} ms; " +
                "jsonreshape-gui.exe window {2} ms, file ready {3} ms, whole run {4} ms"
        Write-Host ($line -f $times.runs, $times.cli_version_ms, $times.gui_window_ms, $times.gui_ready_ms,
                    $times.gui_total_ms)
    }
}
finally {
    Pop-Location
}
