#ifndef PayloadRoot
  #error PayloadRoot must point to the collected App directory.
#endif
#ifndef InstallerOutput
  #error InstallerOutput must point to the output directory.
#endif

[Setup]
AppId={{4F271ABA-D768-42B2-9A96-CE947AD56CBB}
AppName=蓝豆指纹浏览器
AppVersion=1.0.0
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
CloseApplications=no
RestartApplications=no
UsePreviousTasks=no
; The automated test mode keeps desktop and Windows app registration untouched.
CreateUninstallRegKey=not IsAcceptanceTest

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："

[Files]
Source: "{#PayloadRoot}\*"; DestDir: "{app}"; Excludes: "__pycache__\*,*.pyc,*.log,*.dmp,uninstall.ps1"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\蓝豆指纹浏览器"; Filename: "{app}\BlueBean.exe"; WorkingDir: "{app}"; IconFilename: "{app}\landou-browser.ico"; Check: not IsAcceptanceTest
Name: "{autodesktop}\蓝豆指纹浏览器"; Filename: "{app}\BlueBean.exe"; WorkingDir: "{app}"; IconFilename: "{app}\landou-browser.ico"; Tasks: desktopicon; Check: not IsAcceptanceTest

[Run]
Filename: "{app}\BlueBean.exe"; Description: "启动蓝豆指纹浏览器"; Flags: nowait postinstall skipifsilent

[Code]
function IsAcceptanceTest: Boolean;
begin
  Result := ExpandConstant('{param:ACCEPTANCETEST|0}') = '1';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then begin
    { Chromium's sandbox also needs ALL RESTRICTED APPLICATION PACKAGES. }
    if not Exec(ExpandConstant('{sys}\icacls.exe'),
      '"' + ExpandConstant('{app}') + '" /grant *S-1-15-2-1:(OI)(CI)(RX) *S-1-15-2-2:(OI)(CI)(RX) /T /C',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
      RaiseException('无法配置浏览器沙箱的文件访问权限。');
    if ResultCode <> 0 then
      RaiseException('浏览器沙箱权限配置失败，请检查安装目录权限。');
  end;
end;
