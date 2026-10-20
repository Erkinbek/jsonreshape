; Inno Setup 6 script of the jsonreshape Windows installer (docs/ARCHITECTURE.md section 16 "Distribution").
;
; Compiled by scripts/build_exe.ps1 -Installer after the PyInstaller build (ISCC.exe from the PATH or from
; "%ProgramFiles(x86)%\Inno Setup 6"), with the values of packaging/version_info.py passed as defines:
;
;   ISCC.exe /DAppVersion=0.1.0 /DAppVersionNumeric=0.1.0.0 "/DAppCopyright=(c) 2026 Erkin Pardayev"
;            /DSourceDir=<root>\dist\jsonreshape /DOutputDir=<root>\dist
;            /DOutputBaseFilename=jsonreshape-0.1.0-setup-x64 packaging\installer.iss
;
; Without /DAppVersion the compilation stops (#error): the version is never typed here by hand. Everything else has a
; default for a manual compilation from the project root (paths are relative to this file's folder).
;
; What it installs: dist\jsonreshape\* (jsonreshape-gui.exe, jsonreshape.exe and the shared _internal\ folder) into
; {autopf}\jsonreshape -- "Program Files" when installed for all users, "%LOCALAPPDATA%\Programs" for the current
; user (the installer asks; /ALLUSERS and /CURRENTUSER decide it silently) -- a Start menu shortcut of the GUI, an
; optional desktop shortcut, optionally the folder on the user's PATH (for the command line; the uninstaller removes
; it again), and the uninstaller. Nothing else is written: no file associations (the program's Settings > General
; "Open with" does that per user, jsonreshape/gui/osintegration.py), no services, no network access.
;
; AppId is fixed (generated once, 2026-10-20) so that every version upgrades the same installation; the uninstall
; registry key "{AppId}_is1" is the ProductCode of the winget manifests (packaging/winget).
; tests/test_packaging_pipeline.py checks this script statically (sections, defines, the GUID shared with winget).

#ifndef AppVersion
  #error AppVersion is not defined: compile with /DAppVersion=<version> (scripts\build_exe.ps1 -Installer does it)
#endif
#ifndef AppVersionNumeric
  #define AppVersionNumeric AppVersion
#endif
#ifndef AppCopyright
  #define AppCopyright "(c) 2026 Erkin Pardayev"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\jsonreshape"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#ifndef OutputBaseFilename
  #define OutputBaseFilename "jsonreshape-" + AppVersion + "-setup-x64"
#endif
#ifndef IconFile
  #define IconFile "..\build\jsonreshape.ico"
#endif

#define AppName "JSON Reshape"
#define AppPublisher "Erkin Pardayev"
#define AppURL "https://github.com/Erkinbek/jsonreshape"
#define AppExeName "jsonreshape-gui.exe"
#define CliExeName "jsonreshape.exe"

[Setup]
AppId={{FA95975A-1001-43C5-8BE8-53ADDEF4E518}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
AppCopyright={#AppCopyright}
VersionInfoVersion={#AppVersionNumeric}
VersionInfoProductTextVersion={#AppVersion}
VersionInfoDescription={#AppName} installer
VersionInfoCopyright={#AppCopyright}
DefaultDirName={autopf}\jsonreshape
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes
LicenseFile=..\LICENSE
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}
#ifexist IconFile
SetupIconFile={#IconFile}
#endif
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ChangesEnvironment=yes
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
english.AddToPath=Add the command-line tool ({#CliExeName}) to the PATH of this user
english.CommandLine=Command line:

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "addtopath"; Description: "{cm:AddToPath}"; GroupDescription: "{cm:CommandLine}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Comment: "Viewer, editor and reshaper for huge JSON files"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
; the user's PATH (HKCU\Environment, REG_EXPAND_SZ): appended once, removed by the uninstaller ([Code])
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Tasks: addtopath; Check: NeedsAddPath(ExpandConstant('{app}'))

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

; The program's own data (%LOCALAPPDATA%\jsonreshape: settings, logs, caches, edit layers) is the user's and is not
; deleted by the uninstaller.

[Code]
const
  EnvironmentKey = 'Environment';

function NeedsAddPath(Param: string): Boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  { the folder with a semicolon on both sides: not found means it must be added }
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;

procedure RemoveFromPath(Path: string);
var
  Paths: string;
  P: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Paths) then
    exit;
  P := Pos(';' + Uppercase(Path) + ';', ';' + Uppercase(Paths) + ';');
  if P = 0 then
    exit;
  { P counts the semicolon prepended above: the entry starts at P - 1 in Paths (at 1 when it is the first one) }
  if P = 1 then
    Delete(Paths, 1, Length(Path) + 1)
  else
    Delete(Paths, P - 1, Length(Path) + 1);
  RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Paths);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromPath(ExpandConstant('{app}'));
end;
