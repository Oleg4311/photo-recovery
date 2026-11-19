#define AppName "Photo Recovery"
#define AppVersion "0.7"
#define AppExe "PhotoRecovery.exe"

[Setup]
AppId={{291E3418-F5D9-4A36-B071-BBDA68299AF4}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Photo Recovery
DefaultDirName={localappdata}\Programs\Photo Recovery
DefaultGroupName=Photo Recovery
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.22000
OutputDir=..\release
OutputBaseFilename=PhotoRecovery-0.7-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExe}
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"

[Files]
Source: "..\dist\PhotoRecovery\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist\PhotoRecoveryCLI\*"; DestDir: "{app}\CLI"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\benchmark\V05-REPORT.md"; DestDir: "{app}\Документация"; Flags: ignoreversion
Source: "README.txt"; DestDir: "{app}\Документация"; Flags: ignoreversion
Source: "..\V06-REPORT.md"; DestDir: "{app}\Документация"; Flags: ignoreversion
Source: "..\V07-REPORT.md"; DestDir: "{app}\Документация"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Photo Recovery"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{autodesktop}\Photo Recovery"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Запустить Photo Recovery"; Flags: nowait postinstall skipifsilent
