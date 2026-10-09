#define MyAppName "SchoolHub"
#define MyAppVersion "2.6.3"
#define MyAppPublisher "SchoolHub"
#define MyAppExeName "SchoolHub.exe"

[Setup]
AppId={{D4537BD6-45BC-4D88-8A0E-3AF19B8E4B4F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://github.com/MeloniMirko/SchoolHub
AppSupportURL=https://github.com/MeloniMirko/SchoolHub
AppUpdatesURL=https://github.com/MeloniMirko/SchoolHub/releases
DefaultDirName={localappdata}\Programs\SchoolHub
DefaultGroupName=SchoolHub
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\..\dist
OutputBaseFilename=SchoolHub-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
CloseApplicationsFilter=SchoolHub.exe
RestartApplications=no
SetupLogging=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
VersionInfoVersion=2.6.3.0
VersionInfoProductName=SchoolHub
VersionInfoDescription=SchoolHub Installer
VersionInfoCompany=SchoolHub
VersionInfoCopyright=Copyright © 2026 SchoolHub
VersionInfoProductVersion=2.6.3.0

[Languages]
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"

[Tasks]
Name: "desktopicon"; Description: "Crea un'icona sul desktop"; GroupDescription: "Collegamenti:"; Flags: unchecked

[Files]
Source: "..\..\dist\SchoolHub.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\SchoolHub"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\SchoolHub"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Avvia SchoolHub"; Flags: nowait postinstall skipifsilent

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Answer: Integer;
  Confirm: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Answer := MsgBox(
      'SchoolHub è stato disinstallato.' + #13#10 + #13#10 +
      'Per sicurezza, Vault, configurazione, backup e file scolastici NON sono stati eliminati.' + #13#10 + #13#10 +
      'Vuoi eliminare anche TUTTI i dati locali di SchoolHub?',
      mbConfirmation,
      MB_YESNO or MB_DEFBUTTON2
    );

    if Answer = IDYES then
    begin
      Confirm := MsgBox(
        'ATTENZIONE: questa operazione elimina definitivamente il Vault locale, configurazione, log, backup e Workspace di SchoolHub.' + #13#10 + #13#10 +
        'Assicurati che i file importanti siano già sincronizzati o copiati altrove.' + #13#10 + #13#10 +
        'Confermi la cancellazione definitiva?',
        mbError,
        MB_YESNO or MB_DEFBUTTON2
      );

      if Confirm = IDYES then
      begin
        DelTree(ExpandConstant('{localappdata}\SchoolHub'), True, True, True);
      end;
    end;
  end;
end;
