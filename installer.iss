#define MyAppName "医学题库智能解析"
#define MyAppVersion "2.2.1"
#define MyAppExeName "MedExplainStudio.exe"

[Setup]
AppId={{B4EBDA72-14A0-4A67-9A3B-14A09F7F5925}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={autopf}\MedExplainStudio
DefaultGroupName={#MyAppName}
OutputDir=installer-output
OutputBaseFilename=MedExplainStudio-Setup
; 安装包约 1.5GB：单文件 exe 启动前会被系统/杀软全量校验，双击后长时间无窗口。
; 分盘后 exe 仅为几 MB 启动器（秒开向导），数据在旁边的 -1.bin，安装时才读取。
DiskSpanning=yes
DiskSliceSize=2100000000
Compression=lzma2
SolidCompression=yes
; Parallel build compression; the delivered installer remains a small EXE + BIN.
LZMANumBlockThreads=4
LZMAUseSeparateProcess=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=assets\app_icon.ico

[Files]
Source: "dist\MedExplainStudio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; 2.2.0 cloud-only migration: remove only app-owned obsolete AI resources.
; User data lives outside {app}; previous installers remain available for recovery.
[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal\models"
Type: filesandordirs; Name: "{app}\_internal\docling"
Type: filesandordirs; Name: "{app}\_internal\docling_core"
Type: filesandordirs; Name: "{app}\_internal\docling_parse"
Type: filesandordirs; Name: "{app}\_internal\rapidocr"
Type: filesandordirs; Name: "{app}\_internal\onnxruntime"
Type: filesandordirs; Name: "{app}\_internal\torch"
Type: filesandordirs; Name: "{app}\_internal\torchvision"
Type: filesandordirs; Name: "{app}\_internal\transformers"

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent
