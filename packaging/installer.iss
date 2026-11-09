; Inno Setup 6 script for MirrorBridge-Setup-<version>.exe. The release workflow builds it:
;   iscc /DAppVersion=0.4.0 /DExeDir=dist /DNoticesDir=build\notices /Orelease-installer packaging\installer.iss
;
; It installs for the current user only (%LOCALAPPDATA%\Programs\MirrorBridge, no administrator
; rights, no UAC prompt), so the app can update itself the same way: mirrorbridge/update.py starts
; it with /SILENT /SUPPRESSMSGBOXES /UPDATE [/RELAUNCH]. With /UPDATE it waits (up to 90 s) for
; the running MirrorBridge to quit and for MirrorBridge.exe to be free, then replaces it; with
; /RELAUNCH it starts the new one. Settings and pairings stay in %APPDATA%\MirrorBridge, which
; neither installing nor uninstalling touches.

#ifndef AppVersion
  #error Pass /DAppVersion=X.Y.Z (packaging/check_version.py prints it)
#endif
#ifndef ExeDir
  #define ExeDir "..\dist"
#endif
#ifndef NoticesDir
  #define NoticesDir "..\build\notices"
#endif
#define AppMutex "MirrorBridgeAppMutex"

[Setup]
; Never change the AppId: it is how a new version finds the one installed (mirrorbridge/update.py
; reads its uninstall entry too).
AppId={{8F1C2D7A-4B3E-4E59-9C1A-6D2B5E7F9A10}
AppName=MirrorBridge
AppVersion={#AppVersion}
AppVerName=MirrorBridge {#AppVersion}
AppPublisher=draczer01
AppPublisherURL=https://github.com/draczer01/mirrorbridge
AppSupportURL=https://github.com/draczer01/mirrorbridge/issues
AppUpdatesURL=https://github.com/draczer01/mirrorbridge/releases
VersionInfoVersion={#AppVersion}.0
VersionInfoProductName=MirrorBridge
VersionInfoDescription=MirrorBridge installer
PrivilegesRequired=lowest
DefaultDirName={autopf}\MirrorBridge
DisableDirPage=auto
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
UninstallDisplayIcon={app}\MirrorBridge.exe
UninstallDisplayName=MirrorBridge
SetupIconFile=mirrorbridge.ico
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; MirrorBridge.exe is already compressed inside; this keeps the installer small without slowing it.
Compression=lzma2/normal
SolidCompression=yes
OutputBaseFilename=MirrorBridge-Setup-{#AppVersion}
; The [Code] below waits for MirrorBridge itself; Windows' Restart Manager would only see the
; launcher process of the one-file exe.
CloseApplications=no
RestartApplications=no
SetupLogging=yes
ShowLanguageDialog=auto

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

[CustomMessages]
en.CloseFirst=MirrorBridge is open. Quit it (its icon next to the clock > Quit), then click Retry.
es.CloseFirst=MirrorBridge está abierto. Ciérralo (su icono junto al reloj > Salir) y pulsa Reintentar.
en.StillBusy=MirrorBridge.exe is still in use. Close every MirrorBridge window, then click Retry.
es.StillBusy=MirrorBridge.exe sigue en uso. Cierra todas las ventanas de MirrorBridge y pulsa Reintentar.
en.NewerInstalled=MirrorBridge %1 is installed, newer than this installer (%2). Nothing was changed.
es.NewerInstalled=Ya está instalado MirrorBridge %1, más nuevo que este instalador (%2). No se cambió nada.
en.UpdateFrom=Update MirrorBridge %1 to %2 (your settings and TV pairings stay)
es.UpdateFrom=Actualizar MirrorBridge %1 a %2 (tus ajustes y emparejamientos con la TV se conservan)
en.Reinstall=Install MirrorBridge %1 again over the installed copy (a repair; settings stay)
es.Reinstall=Volver a instalar MirrorBridge %1 sobre la copia instalada (una reparación; los ajustes se conservan)
en.UpdateButton=&Update
es.UpdateButton=&Actualizar
en.StartApp=Start MirrorBridge
es.StartApp=Abrir MirrorBridge

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#ExeDir}\MirrorBridge.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LEEME.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#NoticesDir}\THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#NoticesDir}\licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion recursesubdirs

[InstallDelete]
; Licence texts of an older version that this one no longer bundles.
Type: filesandordirs; Name: "{app}\licenses"

[Icons]
Name: "{autoprograms}\MirrorBridge"; Filename: "{app}\MirrorBridge.exe"
Name: "{autodesktop}\MirrorBridge"; Filename: "{app}\MirrorBridge.exe"; Tasks: desktopicon

[Run]
; Installed by hand: the last page offers to start it. Updated from the app: it starts again.
Filename: "{app}\MirrorBridge.exe"; Parameters: "gui --open"; Description: "{cm:StartApp}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\MirrorBridge.exe"; Flags: nowait; Check: Relaunch

[Registry]
; Start with Windows (More options in the app) is the app's own switch; uninstalling turns it off.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; ValueName: "MirrorBridge"; Flags: dontcreatekey uninsdeletevalue

[UninstallDelete]
Type: filesandordirs; Name: "{app}\licenses"

[Code]
function HasParam(const Name: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), Name) = 0 then
    begin
      Result := True;
      Exit;
    end;
end;

function Relaunch: Boolean;
begin
  Result := HasParam('/RELAUNCH');
end;

{ True once no MirrorBridge holds its mutex, waiting up to Seconds for that. }
function WaitForAppToQuit(Seconds: Integer): Boolean;
var
  Waited: Integer;
begin
  Waited := 0;
  while CheckForMutexes('{#AppMutex}') and (Waited < Seconds * 4) do
  begin
    Sleep(250);
    Waited := Waited + 1;
  end;
  Result := not CheckForMutexes('{#AppMutex}');
end;

{ True when nothing has FileName open (a running exe keeps its file open), or it is not there. }
function FileIsFree(const FileName: String): Boolean;
var
  Stream: TFileStream;
begin
  Result := True;
  if not FileExists(FileName) then
    Exit;
  try
    Stream := TFileStream.Create(FileName, fmOpenReadWrite or fmShareExclusive);
    Stream.Free;
  except
    Result := False;
  end;
end;

function WaitForFile(const FileName: String; Seconds: Integer): Boolean;
var
  Waited: Integer;
begin
  Waited := 0;
  while (not FileIsFree(FileName)) and (Waited < Seconds * 4) do
  begin
    Sleep(250);
    Waited := Waited + 1;
  end;
  Result := FileIsFree(FileName);
end;

const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8F1C2D7A-4B3E-4E59-9C1A-6D2B5E7F9A10}_is1';

var
  InstalledVersion: String;

{ The version installed before (its uninstall entry), or '' when MirrorBridge is not installed. Installing
  over it is an update in place: the same AppId, so the same folder (UsePreviousAppDir), never a second copy. }
function PreviousVersion: String;
begin
  if not RegQueryStringValue(HKCU, UninstallKey, 'DisplayVersion', Result) then
    Result := '';
end;

{ -1, 0 or 1 as A is older than, the same as or newer than B (X.Y.Z); 0 when either does not parse. }
function CompareVersions(const A, B: String): Integer;
var
  PA, PB: Int64;
begin
  Result := 0;
  if StrToVersion(A, PA) and StrToVersion(B, PB) then
    Result := ComparePackedVersion(PA, PB);
end;

function InitializeSetup: Boolean;
begin
  Result := True;
  InstalledVersion := PreviousVersion;
  if InstalledVersion <> '' then
    Log('MirrorBridge ' + InstalledVersion + ' is installed: this updates it in place');
  { Never put an older version over a newer one (a stale download, an old update): /ALLOWDOWNGRADE to force it. }
  if (InstalledVersion <> '') and (CompareVersions(InstalledVersion, '{#AppVersion}') > 0) and not HasParam('/ALLOWDOWNGRADE') then
  begin
    SuppressibleMsgBox(FmtMessage(CustomMessage('NewerInstalled'), [InstalledVersion, '{#AppVersion}']), mbInformation, MB_OK, IDOK);
    Result := False;
    Exit;
  end;
  if HasParam('/UPDATE') then
    WaitForAppToQuit(90);
  { By hand, or an update whose MirrorBridge did not quit: ask (silent, the answer is Cancel). }
  while CheckForMutexes('{#AppMutex}') do
    if SuppressibleMsgBox(CustomMessage('CloseFirst'), mbError, MB_RETRYCANCEL, IDCANCEL) <> IDRETRY then
    begin
      Log('MirrorBridge is still running: nothing was installed');
      Result := False;
      Exit;
    end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Exe: String;
begin
  Result := '';
  Exe := ExpandConstant('{app}\MirrorBridge.exe');
  { The one-file exe's launcher can outlive the app by a moment, and a command line copy may run. }
  while not WaitForFile(Exe, 30) do
    if SuppressibleMsgBox(CustomMessage('StillBusy'), mbError, MB_RETRYCANCEL, IDCANCEL) <> IDRETRY then
    begin
      Result := CustomMessage('StillBusy');
      Exit;
    end;
end;

{ Installed already: the last page says Update, and what it updates from. }
procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpReady) and (InstalledVersion <> '') then
    WizardForm.NextButton.Caption := CustomMessage('UpdateButton');
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo, MemoComponentsInfo,
  MemoGroupInfo, MemoTasksInfo: String): String;
begin
  Result := '';
  if InstalledVersion = '' then
    Result := MemoDirInfo
  else if CompareVersions(InstalledVersion, '{#AppVersion}') = 0 then
    Result := FmtMessage(CustomMessage('Reinstall'), ['{#AppVersion}']) + NewLine + NewLine + MemoDirInfo
  else
    Result := FmtMessage(CustomMessage('UpdateFrom'), [InstalledVersion, '{#AppVersion}']) + NewLine + NewLine + MemoDirInfo;
  if MemoTasksInfo <> '' then
    Result := Result + NewLine + NewLine + MemoTasksInfo;
end;

{ Someone who had Start with Windows on for a copy elsewhere (the exe run from Downloads) now gets
  the installed one started instead, as the app's own switch writes it (gui/winintegration.py). }
procedure CurStepChanged(CurStep: TSetupStep);
var
  Command: String;
begin
  if CurStep <> ssPostInstall then
    Exit;
  if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'MirrorBridge', Command) then
    if (Pos('mirrorbridge.exe', Lowercase(Command)) > 0) then
      RegWriteStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'MirrorBridge',
        '"' + ExpandConstant('{app}\MirrorBridge.exe') + '"');
end;

function InitializeUninstall: Boolean;
begin
  Result := True;
  while CheckForMutexes('{#AppMutex}') do
    if SuppressibleMsgBox(CustomMessage('CloseFirst'), mbError, MB_RETRYCANCEL, IDCANCEL) <> IDRETRY then
    begin
      Result := False;
      Exit;
    end;
end;
