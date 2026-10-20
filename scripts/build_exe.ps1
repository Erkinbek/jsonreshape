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
    6. the folder's size, file count and the measured start times;
    7. with -Installer: the Inno Setup installer dist\jsonreshape-<version>-setup-x64.exe from
       packaging\installer.iss (ISCC.exe from the PATH or "%ProgramFiles(x86)%\Inno Setup 6"; the version and the
       copyright of packaging\version_info.py are passed as /D defines). Without ISCC the step explains how to
       install Inno Setup 6 and is skipped;
    8. with -Sign <thumbprint>: both executables (before the tests) and the installer are signed with
       signtool sign /sha1 <thumbprint> /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 (the certificate
       must be in the user's or the machine's certificate store; the timestamp server is network access). Without
       signtool (PATH or the Windows SDK folder) the step explains and the files stay unsigned.

    build\ and dist\ are not committed (.gitignore). Close running copies of the program first (Windows keeps
    the files of a running exe locked). The release workflow (.github/workflows/release.yml) runs this script with
    -SkipTests -NoInstall -Installer (and -Sign when the signing secrets exist).

.PARAMETER Python
    The Python interpreter to build with (default: .venv\Scripts\python.exe of the project).

.PARAMETER SkipTests
    Build only.

.PARAMETER NoInstall
    Fail instead of installing PyInstaller.

.PARAMETER Installer
    Also compile packaging\installer.iss with Inno Setup 6 (ISCC.exe) into dist\jsonreshape-<version>-setup-x64.exe.

.PARAMETER Sign
    SHA-1 thumbprint (40 hex digits) of the code-signing certificate: sign the executables and the installer with
    signtool (Windows SDK).
#>
[CmdletBinding()]
param(
    [string]$Python = "",
    [switch]$SkipTests,
    [switch]$NoInstall,
    [switch]$Installer,
    [string]$Sign = ""
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

function Invoke-Native {
    # another tool (ISCC, signtool) under "Stop": Windows PowerShell 5.1 turns a native command's stderr lines into
    # errors, so it runs under "Continue" and only the exit code decides
    param([string]$What, [string]$Exe, [string[]]$Arguments)
    Write-Host "==> $What"
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Exe @Arguments
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) { throw "$What failed (exit code $code)" }
}

function Find-Iscc {
    # Inno Setup 6's command-line compiler: PATH, then the usual install folders
    $cmd = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($dir in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, (Join-Path $env:LOCALAPPDATA "Programs"))) {
        if (-not $dir) { continue }
        $candidate = Join-Path $dir "Inno Setup 6\ISCC.exe"
        if (Test-Path $candidate) { return $candidate }
    }
    return $null
}

function Find-Signtool {
    # signtool of the Windows SDK: PATH, then the newest x64 one under "Windows Kits\10\bin\<version>\x64"
    $cmd = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $kits = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    if (-not (Test-Path $kits)) { return $null }
    $found = Get-ChildItem -Path $kits -Filter "signtool.exe" -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match "\\x64\\signtool\.exe$" } |
        Sort-Object { try { [version]$_.Directory.Parent.Name } catch { [version]"0.0" } } -Descending |
        Select-Object -First 1
    if ($found) { return $found.FullName }
    return $null
}

function Sign-Files {
    # signtool sign /sha1 <thumbprint> /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 <file>...
    # (the timestamp server is network access: a developer step, nothing the program does). False when signtool is
    # missing: the files stay unsigned and the reason is printed.
    param([string]$Thumbprint, [string[]]$Files)
    $signtool = Find-Signtool
    if (-not $signtool) {
        Write-Warning ("signtool.exe was not found (PATH, '%ProgramFiles(x86)%\Windows Kits\10\bin\<version>\x64'): " +
                       "install the Windows SDK component 'Windows SDK Signing Tools for Desktop Apps' " +
                       "(winget install Microsoft.WindowsSDK.10.0.26100) and run the script again with -Sign. " +
                       "NOT signed: " + (($Files | ForEach-Object { Split-Path -Leaf $_ }) -join ", "))
        return $false
    }
    foreach ($file in $Files) {
        Invoke-Native ("signtool sign " + (Split-Path -Leaf $file) + " (certificate $Thumbprint)") $signtool @(
            "sign", "/sha1", $Thumbprint, "/fd", "SHA256", "/tr", "http://timestamp.digicert.com", "/td", "SHA256",
            $file)
    }
    return $true
}

if ($Sign -and ($Sign -notmatch "^[0-9A-Fa-f]{40}$")) {
    throw "-Sign expects the SHA-1 thumbprint of the certificate (40 hex digits; certmgr.msc or Get-ChildItem Cert:\CurrentUser\My), got '$Sign'"
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

    # 4b. code signing of the two executables (-Sign; the installer is signed after it is built, step 7)
    $signed = $false
    if ($Sign) {
        $signed = Sign-Files $Sign @((Join-Path $Dist "jsonreshape-gui.exe"), (Join-Path $Dist "jsonreshape.exe"))
    }

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
    if ($Sign) {
        if ($signed) { Write-Host "executables signed (certificate $Sign)" } else { Write-Host "executables NOT signed" }
    }

    # 7. the installer (-Installer): packaging\installer.iss with the values of packaging\version_info.py as defines
    if ($Installer) {
        $iscc = Find-Iscc
        if (-not $iscc) {
            Write-Warning ("ISCC.exe (Inno Setup 6) was not found on the PATH or in '%ProgramFiles(x86)%\Inno Setup 6': " +
                           "install it with 'winget install JRSoftware.InnoSetup' (or from https://jrsoftware.org/isdl.php) " +
                           "and run the script again with -Installer. The installer was NOT built.")
        }
        else {
            # JSON with ensure_ascii: the copyright sign (U+00A9) travels escaped, whatever the console's code page
            $infoJson = & $Python -c "import json, sys; sys.path.insert(0, 'packaging'); import version_info as v; ver = v.app_version('.'); print(json.dumps({'version': ver, 'numeric': '.'.join(str(n) for n in v.version_tuple(ver)), 'copyright': v.COPYRIGHT}))"
            if ($LASTEXITCODE -ne 0) { throw "reading packaging\version_info.py failed (exit code $LASTEXITCODE)" }
            $info = ($infoJson | Out-String) | ConvertFrom-Json
            $baseName = "jsonreshape-$($info.version)-setup-x64"
            $installerPath = Join-Path $Root "dist\$baseName.exe"
            Invoke-Native "ISCC packaging\installer.iss -> dist\$baseName.exe" $iscc @(
                "/Q",
                "/DAppVersion=$($info.version)",
                "/DAppVersionNumeric=$($info.numeric)",
                "/DAppCopyright=$($info.copyright)",
                "/DSourceDir=$Dist",
                "/DOutputDir=$(Join-Path $Root 'dist')",
                "/DOutputBaseFilename=$baseName",
                "/DIconFile=$(Join-Path $Build 'jsonreshape.ico')",
                (Join-Path $Root "packaging\installer.iss"))
            if (-not (Test-Path $installerPath)) { throw "ISCC finished but $installerPath does not exist" }
            $installerSigned = $false
            if ($Sign) { $installerSigned = Sign-Files $Sign @($installerPath) }
            $installerMb = [math]::Round((Get-Item $installerPath).Length / 1MB, 1)
            $state = if (-not $Sign) { "" } elseif ($installerSigned) { ", signed" } else { ", NOT signed" }
            Write-Host ("installer: {0} ({1} MB{2})" -f $installerPath, $installerMb, $state)
        }
    }
}
finally {
    Pop-Location
}
