#ifndef PayloadRoot
  #error PayloadRoot must point to the collected App directory.
#endif
#ifndef InstallerOutput
  #error InstallerOutput must point to the output directory.
#endif
#ifndef PermissionHelper
  #error PermissionHelper must point to the native sandbox ACL helper.
#endif
#ifndef ProductVersion
  #define ProductVersion "1.0.4"
#endif

[Setup]
AppId={{4F271ABA-D768-42B2-9A96-CE947AD56CBB}
AppName=蓝豆指纹浏览器
AppVersion={#ProductVersion}
AppPublisher=蓝豆助手
AppCopyright=Copyright (c) 2026 何阳鸿
LicenseFile={#PayloadRoot}\licenses\BlueBean-LICENSE
DefaultDirName={localappdata}\Programs\BlueBean Fingerprint Browser
DefaultGroupName=蓝豆指纹浏览器
DisableProgramGroupPage=yes
DisableDirPage=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19041
OutputDir={#InstallerOutput}
OutputBaseFilename=BlueBean-Setup-Windows-x64
SetupIconFile={#PayloadRoot}\landou-browser.ico
UninstallDisplayIcon={app}\BlueBean.exe
Compression=lzma2/fast
SolidCompression=no
WizardStyle=modern
; Detect locks from our private runtimes before replacing any package files.
; Python services have no windows, so Restart Manager must also be allowed to
; stop unresponsive/background holders after the installer's close-app prompt.
CloseApplications=force
CloseApplicationsFilter=*.exe,*.dll,*.pyd
RestartApplications=no
UsePreviousTasks=no
; The automated test mode keeps desktop and Windows app registration untouched.
CreateUninstallRegKey=not IsAcceptanceTest

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
chinesesimp.ApplicationsFound=蓝豆浏览器或后台组件仍在使用以下文件。请先保存正在进行的任务，再允许安装程序关闭这些程序。关闭后会继续升级，环境、账号和网站数据将保留。
chinesesimp.CloseApplications=保存任务后，自动关闭占用程序(&A)
chinesesimp.ErrorCloseApplications=仍有程序占用安装文件，安装程序未能自动关闭。请先退出蓝豆浏览器及后台组件，再继续安装；不要跳过被占用的文件。

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："

[Files]
Source: "{#PayloadRoot}\*"; DestDir: "{app}"; Excludes: "__pycache__\*,*.pyc,*.log,*.dmp,uninstall.ps1"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PermissionHelper}"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{autoprograms}\蓝豆指纹浏览器"; Filename: "{app}\BlueBean.exe"; WorkingDir: "{app}"; IconFilename: "{app}\landou-browser.ico"; Check: not IsAcceptanceTest
Name: "{autodesktop}\蓝豆指纹浏览器"; Filename: "{app}\BlueBean.exe"; WorkingDir: "{app}"; IconFilename: "{app}\landou-browser.ico"; Tasks: desktopicon; Check: not IsAcceptanceTest

[Run]
Filename: "{app}\BlueBean.exe"; Description: "启动蓝豆指纹浏览器"; Flags: nowait postinstall skipifsilent

[Code]
var
  SandboxPermissionsFailed: Boolean;

function IsAcceptanceTest: Boolean;
begin
  Result := ExpandConstant('{param:ACCEPTANCETEST|0}') = '1';
end;

function GetCustomSetupExitCode: Integer;
begin
  Result := 0;
  if SandboxPermissionsFailed then
    Result := 20;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then begin
    { Chromium's sandbox also needs ALL RESTRICTED APPLICATION PACKAGES. }
    SandboxPermissionsFailed := True;
    if not Exec(ExpandConstant('{tmp}\BlueBean-Permissions.exe'),
      '"' + ExpandConstant('{app}') + '"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
      RaiseException('无法配置浏览器沙箱的文件访问权限。');
    if ResultCode <> 0 then
      RaiseException('浏览器沙箱权限配置失败，请检查安装目录权限。');
    SandboxPermissionsFailed := False;
    Log('Native sandbox file permissions configured successfully.');
  end;
end;
