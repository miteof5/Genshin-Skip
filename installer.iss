; 原神跳一跳 安装包脚本（Inno Setup 6）
; 编译：ISCC.exe installer.iss

#define MyAppName "原神跳一跳"
#define MyAppExeName "Genshin-Skip.exe"
#define MyAppVersion "1.0.1"
#define MyAppPublisher "miteof5"

[Setup]
AppId={{B7C3E9A1-4F2D-4A8E-9C5B-2E6D1F3A8C4D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Genshin-Skip
DefaultGroupName={#MyAppName}
OutputDir=dist\installer
OutputBaseFilename=Genshin-Skip-Setup-{#MyAppVersion}
SetupIconFile=icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式(&D)"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "dist\Genshin-Skip\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "dist\Genshin-Skip\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; shellexec：Inno Setup 6 会以非提升令牌运行 postinstall 条目，
; 而 Genshin-Skip.exe 是 requireAdministrator，用 CreateProcess 启动会报错误 740。
; ShellExecuteEx 会自动弹出 UAC 提升确认（与双击快捷方式体验一致）。
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 {#MyAppName}"; Flags: nowait postinstall skipifsilent shellexec

[UninstallDelete]
Type: filesandordirs; Name: "{app}\data\diag"
Type: files; Name: "{app}\data\skip_debug.log"
