; ============================================================
;  FexCraft - Script de Inno Setup
;  Requisitos antes de compilar (en la misma carpeta que este .iss):
;    1) dist\FexCraft\   (generada por PyInstaller)
;    2) fexcraft.ico     (ícono del instalador)
;  Compilar: abrir con Inno Setup y presionar F9 -> Output\FexCraft-Setup.exe
; ============================================================

#define MyAppName      "FexCraft"
#define MyAppVersion   "1.0"
#define MyAppPublisher "FEX"
#define MyAppExeName   "FexCraft.exe"

[Setup]
; GUID único de la app (no lo cambies entre versiones para que las actualizaciones reemplacen la anterior)
AppId={{8F3C2A71-5B64-4D0E-9A21-FE0C7A5D1B93}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=FexCraft-Setup
SetupIconFile=fexcraft.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Permite elegir instalar para todos los usuarios o solo para el actual
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el escritorio"; GroupDescription: "Accesos directos:"; Flags: unchecked

[Files]
Source: "dist\FexCraft\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Desinstalar {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Ejecutar {#MyAppName}"; Flags: nowait postinstall skipifsilent
