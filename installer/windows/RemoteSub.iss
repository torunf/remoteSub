; RemoteSub installer: one file that installs RemoteSub and chiaki-ng.
; Build with scripts\build_windows.ps1 (it fetches the chiaki-ng installer into redist\ first).

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{6C2F7A3E-1D4B-4F0E-9B7A-52E1C8D3A901}
AppName=RemoteSub
AppVersion={#AppVersion}
AppPublisher=torunf
DefaultDirName={autopf}\RemoteSub
DefaultGroupName=RemoteSub
DisableProgramGroupPage=yes
; chiaki-ng installs per machine, so the installer needs admin rights.
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=RemoteSub-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\RemoteSub.exe

[Languages]
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\..\dist\RemoteSub\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "redist\chiaki-ng-setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: ChiakiWasMissingCheck

[Icons]
Name: "{group}\RemoteSub"; Filename: "{app}\RemoteSub.exe"
Name: "{autodesktop}\RemoteSub"; Filename: "{app}\RemoteSub.exe"; Tasks: desktopicon

[Run]
; First start walks through pairing, chiaki-ng settings, Ollama and the model, as the signed-in user.
Filename: "{app}\RemoteSub.exe"; Parameters: "--setup"; Description: "RemoteSub'ı başlat ve kurulumu tamamla"; \
  Flags: postinstall nowait skipifsilent runasoriginaluser

[Code]
function ChiakiPath(): String;
var
  Location: String;
begin
  Result := ExpandConstant('{commonpf64}\chiaki-ng\chiaki.exe');
  if FileExists(Result) then Exit;
  Result := ExpandConstant('{commonpf32}\chiaki-ng\chiaki.exe');
  if FileExists(Result) then Exit;
  { chiaki-ng's own Inno Setup AppId }
  if RegQueryStringValue(HKLM64, 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{A329DCDE-074D-4C82-959A-3CFAC9A26B1F}_is1',
                         'InstallLocation', Location) then
  begin
    Result := AddBackslash(Location) + 'chiaki.exe';
    if FileExists(Result) then Exit;
  end;
  Result := '';
end;

var
  ChiakiWasMissing: Boolean;

function ChiakiMissing(): Boolean;
begin
  Result := ChiakiPath() = '';
end;

function ChiakiWasMissingCheck(): Boolean;
begin
  Result := ChiakiWasMissing;
end;

function InitializeSetup(): Boolean;
begin
  { Decided once up front: the [Files] Check and the install below must agree. }
  ChiakiWasMissing := ChiakiMissing();
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if (CurStep = ssPostInstall) and ChiakiWasMissing then
  begin
    WizardForm.StatusLabel.Caption := 'chiaki-ng kuruluyor…';
    Exec(ExpandConstant('{tmp}\chiaki-ng-setup.exe'), '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-',
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    if ChiakiMissing() then
      MsgBox('chiaki-ng kurulamadı (kod ' + IntToStr(ResultCode) + '). ' +
             'RemoteSub açıldığında kurulum penceresinden tekrar denenebilir.', mbError, MB_OK);
  end;
end;
