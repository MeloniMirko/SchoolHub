#define MyAppName "SchoolHub"
#define MyAppVersion "2.5.0"
#define MyAppPublisher "SchoolHub"
#define MyAppExeName "SchoolHub.exe"

[Setup]
AppId={{D4537BD6-45BC-4D88-8A0E-3AF19B8E4B4F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
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
VersionInfoVersion=2.5.0.0
VersionInfoProductName=SchoolHub
VersionInfoDescription=SchoolHub Installer

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
