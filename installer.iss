; ccodex-sleep-plus Inno Setup script
; Build: ISCC.exe installer.iss  (after build_exe.py has produced dist/ccodex-sleep-plus/)
#define MyAppName "ccodex sleep plus"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "ccodex-sleep-plus"
#define MyAppExeName "ccodex-sleep-plus.exe"

[Setup]
AppId={{7C1B9E2A-52C4-4B7D-9A34-CCODEXSLEEPPLUS}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\ccodex-sleep-plus
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=setup
OutputBaseFilename=ccodex-sleep-plus-setup
SetupIconFile=app.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "dist\ccodex-sleep-plus\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs

[Icons]
Name: "{autodesktop}\ccodex sleep plus"; Filename: "{app}\{#MyAppExeName}"; Parameters: "smart"; Tasks: desktopicon
Name: "{group}\ccodex sleep plus"; Filename: "{app}\{#MyAppExeName}"; Parameters: "smart"
Name: "{group}\卸载 ccodex sleep plus"; Filename: "{uninstallexe}"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "install"; Description: "识别 Codex 与代理并启动防护"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 先恢复 Codex 配置再删除程序
Filename: "{app}\{#MyAppExeName}"; Parameters: "stop"; RunOnceId: "RestoreCodex"; Flags: runhidden
