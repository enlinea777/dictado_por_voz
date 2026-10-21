; Instalador de Dictado por Voz para Windows (Inno Setup)
; Se compila con:  iscc /DMyAppVersion=1.1.0 packaging\windows\dictado.iss
; Requiere tener Dictado.exe y dictado.ico en esta carpeta (los copia el CI).

#ifndef MyAppVersion
  #define MyAppVersion "1.1.0"
#endif

#define MyAppName "Dictado por Voz"
#define MyAppPublisher "enlinea777"
#define MyAppURL "https://github.com/enlinea777/dictado_por_voz"
#define MyAppExeName "Dictado.exe"

[Setup]
AppId={{6F2C4A31-9C1E-4C7B-9D2A-3E5B8A7C1D22}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\Dictado
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=dictado-{#MyAppVersion}-setup
SetupIconFile=dictado.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el escritorio"; \
    GroupDescription: "Accesos directos:"; Flags: unchecked

[Files]
Source: "Dictado.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Desinstalar {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; \
    Description: "Ejecutar {#MyAppName}"; Flags: nowait postinstall skipifsilent
