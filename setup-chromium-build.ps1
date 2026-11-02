#Requires -Version 5.1
<#
.SYNOPSIS
    Chromium Windows 构建环境一键部署（VS 2026 + Win11 SDK + depot_tools + 源码 + 编译）

.DESCRIPTION
    幂等脚本：重复运行会跳过已完成步骤，断线后直接重跑同一阶段即可。
    每个阶段独立可跑，失败不污染后续阶段。

.PARAMETER Stage
    prereq   系统前置：长路径支持、内存/磁盘检查
    vs       Visual Studio 2026 + NativeDesktop + ATLMFC + Win11 SDK
    sdk      单独补装 Windows SDK Debugging Tools
    deptools depot_tools 克隆 + 环境变量
    fetch    拉取 Chromium 源码（--nohooks --no-history，省 60GB）
    hooks    gclient runhooks，下载工具链/依赖
    gen      gn gen 生成 ninja 文件
    build    autoninja 编译 chrome
    all      按上述顺序全跑

.PARAMETER BuildType
    standard  is_official_build=false，链接峰值 ~15-20GB，32GB 内存稳
    official  is_official_build=true，接近发行版，链接峰值 20-26GB
    component is_component_build=true，链接峰值 2-3GB，最省内存但运行慢

.PARAMETER SrcRoot
    Chromium checkout 根目录，默认 E:\chromium

.NOTES
    VS 2026 下载入口：https://aka.ms/vs/stable/vs_community.exe
    注意 aka.ms/vs/18/* 短链已失效（重定向到 bing），不要用。
#>
[CmdletBinding()]
param(
    [ValidateSet('all','prereq','proxy','vs','sdk','deptools','fetch','hooks','gen','build')]
    [string]$Stage = 'all',

    [ValidateSet('standard','official','component')]
    [string]$BuildType = 'standard',

    [string]$SrcRoot = 'E:\chromium',
    [string]$DepotTools = 'C:\src\depot_tools',

    # VS 三项安装位置都能挪出 C 盘（官方文档：仅首次安装时可改）
    # 注意 Installer 自身固定留在 C:\Program Files (x86)\Microsoft Visual Studio\Installer，约 1GB
    [string]$InstallRoot = 'E:\Microsoft Visual Studio\2026\Community',
    [string]$CacheRoot   = 'E:\VSCache',
    [string]$SharedRoot  = 'E:\VSShared',

    # direct = 微软 CDN + GitHub 全直连，只有 clang 工具链走代理
    # proxy  = 全走代理（默认）。注意 VS Installer 用 WinHTTP，必须单独设，见 Invoke-ProxySetup
    [ValidateSet('direct','proxy')]
    [string]$NetworkMode = 'proxy',

    [string]$Proxy = 'http://127.0.0.1:7890'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# ---------------------------------------------------------------- helpers
$script:LogDir = Join-Path $PSScriptRoot 'logs'
if (-not (Test-Path $script:LogDir)) { New-Item -ItemType Directory -Path $script:LogDir -Force | Out-Null }

function Write-Step { param([string]$Msg) Write-Host "`n=== $Msg ===" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Msg) Write-Host "  [OK]   $Msg" -ForegroundColor Green }
function Write-Warn2{ param([string]$Msg) Write-Host "  [WARN] $Msg" -ForegroundColor Yellow }
function Write-Err2 { param([string]$Msg) Write-Host "  [FAIL] $Msg" -ForegroundColor Red }
function Write-Info { param([string]$Msg) Write-Host "         $Msg" -ForegroundColor Gray }

function Assert-Admin {
    $pr = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw '需要管理员权限。右键 PowerShell -> 以管理员身份运行，然后重跑。'
    }
}

function Test-Command2 { param([string]$Name) $null -ne (Get-Command $Name -ErrorAction SilentlyContinue) }

# PATH 刷新：VS 装完后当前进程看不到新变量，必须重新读注册表
function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path','Machine')
    $user    = [Environment]::GetEnvironmentVariable('Path','User')
    $env:Path = (($machine, $user) -join ';') -replace ';;+', ';'
}

# ---------------------------------------------------------------- prereq
function Invoke-Prereq {
    Write-Step 'Stage: prereq — 系统前置检查'

    Assert-Admin
    Write-Ok '管理员权限'

    # 长路径：Chromium 源码树深度超 260 字符，不开必炸
    $lpKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem'
    $cur = (Get-ItemProperty $lpKey -Name LongPathsEnabled -ErrorAction SilentlyContinue).LongPathsEnabled
    if ($cur -ne 1) {
        Set-ItemProperty $lpKey -Name LongPathsEnabled -Value 1 -Type DWord
        Write-Ok 'LongPathsEnabled 已开启（原先关闭）'
    } else {
        Write-Ok 'LongPathsEnabled 已经是 1'
    }

    # 内存
    $ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB, 1)
    if ($ramGB -lt 20) {
        Write-Warn2 "物理内存 $ramGB GB，低于 32GB，必须用 -BuildType component"
    } else {
        Write-Ok "物理内存 $ramGB GB"
    }

    # 磁盘：Chromium 官方要求 100GB
    $drive = (Split-Path -Qualifier $SrcRoot).TrimEnd(':')
    $freeGB = [math]::Round((Get-Volume -DriveLetter $drive).SizeRemaining/1GB, 1)
    if ($freeGB -lt 120) {
        throw ("{0}: 只剩 {1} GB，Chromium 需要 100GB+（含 out 目录建议 150GB）" -f $drive, $freeGB)
    }
    Write-Ok ("{0}: 剩余 {1} GB" -f $drive, $freeGB)

    # 基础工具
    foreach ($t in @('git','python')) {
        if (Test-Command2 $t) { Write-Ok "$t -> $((Get-Command $t).Source)" }
        else { throw "缺少 $t，先装：git https://git-scm.com/download/win / python https://www.python.org/downloads/" }
    }

    # 代理：gclient sync 需要访问 googlesource
    if ($Proxy) {
        try {
            $c = New-Object Net.Sockets.TcpClient
            $c.Connect('127.0.0.1', ([uri]$Proxy).Port)
            $c.Close()
            Write-Ok "代理 $Proxy 可连接"
        } catch {
            Write-Warn2 "代理 $Proxy 连不上，gclient sync 会失败。先启动 Clash 或改 -Proxy"
        }
    }
}

# ---------------------------------------------------------------- proxy
<#
    四种工具读代理的位置各不相同，漏一个就会表现为「下载卡住」：
      1. WinHTTP      -> VS Installer / Windows 组件安装器（netsh winhttp）
      2. WinINET      -> 浏览器、部分 .NET 程序（注册表 Internet Settings）
      3. 环境变量      -> CIPD、vpython、curl、大部分现代 CLI
      4. git config   -> git fetch/clone（gclient 底层就是 git）
    系统的 WinHTTP 默认是 Direct，所以不显式设置的话 VS 会走直连。
#>
function Invoke-ProxySetup {
    Write-Step "Stage: proxy — 配置 $Proxy 到四套通道"
    Assert-Admin

    if ($NetworkMode -eq 'direct') {
        Write-Warn2 'NetworkMode=direct，跳过代理配置（仅 clang 阶段会自动挂代理）'
        return
    }

    # 1. WinHTTP — VS Installer 走这个
    $hostPort = ([uri]$Proxy).Authority
    & netsh winhttp set proxy $hostPort 2>&1 | Out-Null
    $wh = (netsh winhttp show proxy) -join ' '
    if ($wh -match [regex]::Escape($hostPort)) { Write-Ok "WinHTTP -> $hostPort （VS Installer 生效）" }
    else { Write-Warn2 "WinHTTP 设置未确认，当前: $wh" }

    # 2. WinINET — 已有则不动
    $ie = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
    if ($ie.ProxyEnable -eq 1 -and $ie.ProxyServer -eq $hostPort) { Write-Ok "WinINET 已经是 $hostPort" }
    else {
        Set-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -Name ProxyEnable -Value 1
        Set-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -Name ProxyServer -Value $hostPort
        Write-Ok "WinINET -> $hostPort"
    }

    # 3. 环境变量 — CIPD / vpython / curl 都认
    foreach ($v in @('HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY','all_proxy')) {
        [Environment]::SetEnvironmentVariable($v, $Proxy, 'Machine')
        Set-Item -Path "env:$v" -Value $Proxy
    }
    Write-Ok "环境变量 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY 已写入（机器级）"

    # 4. git — gclient 底层
    & git config --global http.proxy $Proxy
    & git config --global https.proxy $Proxy
    Write-Ok "git 全局代理已设置"

    Write-Step '代理连通性验证（四类目标各测一个）'
    $curl = "$env:SystemRoot\System32\curl.exe"
    $targets = @(
        @{n='googlesource (源码)'; u='https://chromium.googlesource.com/chromium/src/+/refs/heads/main?format=TEXT'},
        @{n='GCS (CIPD/clang)';    u='https://storage.googleapis.com/chromium-browser-snapshots/Win_x64/LAST_CHANGE'},
        @{n='微软 CDN (VS)';        u='https://aka.ms/vs/stable/vs_community.exe'},
        @{n='GitHub';              u='https://api.github.com/rate_limit'}
    )
    foreach ($t in $targets) {
        $r = & $curl -sL -x $Proxy -o NUL -w "%{http_code}|%{speed_download}" --max-time 25 $t.u 2>&1
        if ($r -match '^(200|206|302)') { Write-Ok ("{0,-24} {1}" -f $t.n, $r) }
        else { Write-Warn2 ("{0,-24} {1}  该源可能不通，相关阶段会失败" -f $t.n, $r) }
    }
}

# ---------------------------------------------------------------- vs
function Invoke-Vs {
    Write-Step 'Stage: vs — 安装 Visual Studio 2026 + C++ 工作负载'

    Assert-Admin

    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $installed = & $vswhere -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationVersion 2>$null
        if ($installed) {
            Write-Ok "已装 VS，版本 $installed"
            return
        }
        Write-Info 'VS 存在但缺 C++ 工具集，继续安装组件'
    }

    # aka.ms/vs/18/* 是死的（跳到 bing），stable 别名才是 VS 2026
    $bootUrl = 'https://aka.ms/vs/stable/vs_community.exe'
    $boot = Join-Path $env:TEMP 'vs_community_boot.exe'

    if (-not (Test-Path $boot)) {
        Write-Info "下载 VS 2026 bootstrapper (5.4MB)"
        $args = @('-sL','--max-time','180','-o',$boot,'-w','HTTP=%{http_code} BYTES=%{size_download}')
        if ($NetworkMode -eq 'proxy') { $args = @('-x',$Proxy) + $args }
        else { $args = @('--noproxy','*') + $args }   # 微软 CDN 国内有节点，直连更快且免费
        $r = & curl.exe @args $bootUrl
        Write-Info $r
        if ($r -notmatch 'HTTP=200') { throw 'VS bootstrapper 下载失败。若直连不通，改 -NetworkMode proxy' }
    }

    $ver = (Get-Item $boot).VersionInfo
    $sig = Get-AuthenticodeSignature $boot
    if ($sig.Status -ne 'Valid') { throw "bootstrapper 签名无效: $($sig.Status)" }
    Write-Ok "bootstrapper $($ver.ProductVersion) / $($ver.FileVersion) 签名有效"

    # NativeDesktop 带 MSVC + CMake + 默认 Windows SDK；ATLMFC 是 Chromium 硬要求
    # 三个路径参数把 IDE / 下载缓存 / 共享 SDK 全部挪到 E 盘
    foreach ($d in @($InstallRoot, $CacheRoot, $SharedRoot)) {
        if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
    }

    # 正确写法是 --path <name>=<path>，支持的 name 只有 install / cache / shared。
    # 踩过的坑：--sharedInstallPath 不存在（setup.exe 直接 87 = ERROR_INVALID_PARAMETER）；
    #           --cache 是布尔开关（「装完保留包」），不接受路径，路径必须走 --path cache=<dir>。
    # 而且 --path install=<dir> 与 --installPath 等价、不能同时出现，所以这里统一用 --path。
    $argString = @(
        "--path install=`"$InstallRoot`""
        "--path cache=`"$CacheRoot`""
        "--path shared=`"$SharedRoot`""
        '--add Microsoft.VisualStudio.Workload.NativeDesktop'
        '--add Microsoft.VisualStudio.Component.VC.ATLMFC'
        '--includeRecommended'
        '--quiet'
        '--wait'
        '--norestart'
    ) -join ' '

    Write-Info "IDE      -> $InstallRoot"
    Write-Info "下载缓存  -> $CacheRoot   （装完可整个删掉）"
    Write-Info "共享 SDK -> $SharedRoot"
    Write-Info 'C 盘只保留 Installer 本体约 1GB'
    Write-Info '下载约 3-6GB，经 clash 代理（WinHTTP 已指向 7890）。'
    Write-Info '用 Get-Process vs_installer,setup -ErrorAction SilentlyContinue 观察进度'
    Write-Host "  cmd: `"$boot`" $argString" -ForegroundColor DarkGray
    $log = Join-Path $script:LogDir 'vs-install.log'
    $p = Start-Process -FilePath $boot -ArgumentList $argString -Wait -PassThru
    "ExitCode=$($p.ExitCode)`nArgs=$argString" | Out-File $log -Encoding utf8

    # 3010 = 需要重启，安装本身成功
    if ($p.ExitCode -in 0, 3010) {
        Write-Ok "VS 安装退出码 $($p.ExitCode)"
    } else {
        $hint = switch ($p.ExitCode) {
            87   { '参数错误。--path 的 name 只能是 install/cache/shared，且 install 不能与 --installPath 同时出现' }
            5005 { 'bootstrapper 命令行解析失败，检查引号与空格' }
            5007 { '机器不满足要求（磁盘/系统版本）' }
            8004 { '目标目录失败，检查 E 盘权限与剩余空间' }
            default { '详见 %TEMP%\dd_installer_*.log 与 dd_setup_*.log' }
        }
        throw "VS 安装失败，退出码 $($p.ExitCode) — $hint"
    }

    Update-SessionPath
    if (Test-Path $vswhere) {
        $paths = & $vswhere -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
        Write-Ok "MSVC 安装于 $paths"
    }
}

# ---------------------------------------------------------------- sdk
function Invoke-Sdk {
    Write-Step 'Stage: sdk — 验证 Windows SDK 与调试工具'

    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path $vswhere)) {
        Write-Warn2 'VS 未安装，跳过。先跑 -Stage vs'
        return
    }

    # SDK 真实位置从注册表读。--path shared 会把 SDK 装到 <shared 所在盘>\Windows Kits\10，
    # 而不是 C:\Program Files (x86)\Windows Kits\10 —— 这里踩过：SDK 落在 E:\Windows Kits\10。
    $kitsRoot = $null
    foreach ($k in @('HKLM:\SOFTWARE\Microsoft\Windows Kits\Installed Roots','HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows Kits\Installed Roots')) {
        $v = (Get-ItemProperty $k -Name KitsRoot10 -ErrorAction SilentlyContinue).KitsRoot10
        if ($v) { $kitsRoot = $v.TrimEnd('\'); break }
    }
    if (-not $kitsRoot) { $kitsRoot = "${env:ProgramFiles(x86)}\Windows Kits\10" }
    Write-Info "KitsRoot10 = $kitsRoot"

    $kits = Join-Path $kitsRoot 'Include'
    if (Test-Path $kits) {
        $vers = Get-ChildItem $kits -Directory | Select-Object -ExpandProperty Name
        Write-Ok "已装 Windows SDK: $($vers -join ', ')"
        if ($vers | Where-Object { $_ -like '10.0.28000*' }) {
            Write-Ok 'SDK 10.0.28000 存在，满足 Chromium main 要求'
        } elseif ($vers | Where-Object { $_ -like '10.0.26100*' }) {
            Write-Warn2 '只有 26100，Chromium main 要 10.0.28000.2270。补装：'
            Write-Info '  vs_community.exe modify --installPath "<vs>" --add Microsoft.VisualStudio.Component.Windows11SDK.28000 --quiet --wait --norestart'
        } else {
            Write-Warn2 "未找到 28000，实际: $($vers -join ', ')"
        }
        # 抽查 28000 的关键文件，避免「目录在但内容是空壳」
        $sdk28 = Join-Path $kits '10.0.28000.0'
        if (Test-Path $sdk28) {
            foreach ($f in @('um\Windows.h','ucrt\stdio.h','shared\windef.h')) {
                if (Test-Path (Join-Path $sdk28 $f)) { Write-Info "  28000 有 $f" } else { Write-Warn2 "  28000 缺 $f" }
            }
        }
    } else {
        Write-Warn2 "$kits 不存在，SDK 未装"
    }

    # 调试工具：读大页 PDB 必需（不影响编译，只影响符号解析）
    $dbg = $null
    foreach ($c in @((Join-Path $kitsRoot 'Debuggers\x64\cdb.exe'), "${env:ProgramFiles(x86)}\Windows Kits\10\Debuggers\x64\cdb.exe")) {
        if (Test-Path $c) { $dbg = $c; break }
    }
    if ($dbg) {
        Write-Ok "Debugging Tools 就位，cdb $((Get-Item $dbg).VersionInfo.FileVersion)"
    } else {
        Write-Warn2 '缺 SDK Debugging Tools（只影响调试大页 PDB，不影响编译）。补装：'
        Write-Info '  控制面板 -> 程序和功能 -> Windows Software Development Kit -> 更改 -> 勾 Debugging Tools for Windows'
    }

    # VS 侧确认：MSVC 工具集 + ATLMFC 是 Chromium 硬要求
    $vsPath = & $vswhere -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
    $atlOk = & $vswhere -products * -requires Microsoft.VisualStudio.Component.VC.ATLMFC -property installationPath 2>$null
    if ($vsPath) { Write-Ok "MSVC x64 工具集: $vsPath" } else { Write-Warn2 'MSVC x64 工具集缺失' }
    if ($atlOk)  { Write-Ok 'ATLMFC 已装（Chromium 硬要求）' } else { Write-Warn2 'ATLMFC 缺失，Chromium 会报 atlbase.h 找不到' }
}

# ---------------------------------------------------------------- deptools
function Invoke-DepotTools {
    Write-Step 'Stage: deptools — depot_tools'

    if (Test-Path (Join-Path $DepotTools 'gclient')) {
        Write-Ok "depot_tools 已存在: $DepotTools"
    } else {
        $parent = Split-Path $DepotTools -Parent
        if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }

        Write-Info "git clone depot_tools -> $DepotTools"
        $gitArgs = @()
        if ($Proxy) { $gitArgs += @('-c', "http.proxy=$Proxy", '-c', "https.proxy=$Proxy") }
        $gitArgs += @('clone', '--depth', '1', 'https://chromium.googlesource.com/chromium/tools/depot_tools.git', $DepotTools)
        & git @gitArgs
        if ($LASTEXITCODE -ne 0) { throw "depot_tools clone 失败 ($LASTEXITCODE)" }
        Write-Ok 'depot_tools 克隆完成'
    }

    # 环境变量（机器级，需要管理员）
    $mPath = [Environment]::GetEnvironmentVariable('Path','Machine')
    if ($mPath -notlike "*$DepotTools*") {
        [Environment]::SetEnvironmentVariable('Path', "$DepotTools;$mPath", 'Machine')
        Write-Ok "PATH 前置加入 $DepotTools"
    } else {
        Write-Ok 'PATH 已含 depot_tools'
    }

    # 0 = 用本地 VS，不要用 Google 内部工具链
    [Environment]::SetEnvironmentVariable('DEPOT_TOOLS_WIN_TOOLCHAIN','0','Machine')
    Write-Ok 'DEPOT_TOOLS_WIN_TOOLCHAIN=0'

    # 让 gclient 明确知道 VS 2026 在哪
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $vsPath = & $vswhere -products * -property installationPath -version '[18.0,19.0)' 2>$null
        if (-not $vsPath) { $vsPath = & $vswhere -products * -property installationPath 2>$null | Select-Object -First 1 }
        if ($vsPath) {
            [Environment]::SetEnvironmentVariable('vs2026_install', $vsPath, 'Machine')
            Write-Ok "vs2026_install=$vsPath"
        }
    }

    # 代理写进 .netrc / git config，让 gclient sync 走代理
    if ($Proxy) {
        & git config --global http.proxy $Proxy
        & git config --global https.proxy $Proxy
        Write-Ok "git 全局代理 = $Proxy"
    }

    Update-SessionPath
    $env:Path = "$DepotTools;$env:Path"
    $env:DEPOT_TOOLS_WIN_TOOLCHAIN = '0'

    if (Test-Command2 gclient) { Write-Ok "gclient -> $((Get-Command gclient).Source)" }
    else { Write-Warn2 '当前会话仍找不到 gclient，新开一个管理员 PowerShell 再继续' }
}

# ---------------------------------------------------------------- fetch
function Invoke-Fetch {
    Write-Step 'Stage: fetch — 拉取 Chromium 源码'

    $env:Path = "$DepotTools;$env:Path"
    $env:DEPOT_TOOLS_WIN_TOOLCHAIN = '0'
    # 0 = 跳过 update_depot_tools.bat 的自更新。否则它每次都去 googlesource 拉最新，
    # 走代理不仅慢，还会覆盖掉手工修好的 .cipd_bin（vpython3 那份是绕过 360 手动解压的）。
    $env:DEPOT_TOOLS_UPDATE = '0'

    # gclient 底层的 git 用 git config 的代理，但 cartographer/cipd 等子工具读环境变量。
    # 机器级变量对已启动的进程无效，这里显式灌进当前会话。
    if ($NetworkMode -eq 'proxy' -and $Proxy) {
        foreach ($v in @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy')) {
            Set-Item -Path "env:$v" -Value $Proxy -ErrorAction SilentlyContinue
        }
        Write-Info "代理环境变量已注入当前会话: $Proxy"
    }

    if (Test-Path (Join-Path $SrcRoot 'src\chrome')) {
        Write-Ok "源码已存在: $SrcRoot\src，跳过 fetch"
        return
    }

    if (-not (Test-Path $SrcRoot)) { New-Item -ItemType Directory -Path $SrcRoot -Force | Out-Null }

    Push-Location $SrcRoot
    try {
        # --nohooks: hooks 单独跑，失败可单独重试
        # --no-history: 不拉完整 git 历史，体积从 ~90GB 降到 ~15GB
        # 注意：新版 fetch.py 只接受它自己那几个开关，透传 -j 会直接 "Got bad arguments" 退出。
        # 并发交给 gclient 自己定（默认按逻辑核数，本机 16 路）。
        Write-Info 'fetch --nohooks --no-history chromium'
        Write-Info '首次约 15-25GB，按 11MB/s 约 30-50 分钟。断线直接重跑本阶段，gclient 是增量的。'
        $sw = [Diagnostics.Stopwatch]::StartNew()
        & cmd /c "fetch --nohooks --no-history chromium 2>&1" | Tee-Object -FilePath (Join-Path $script:LogDir 'fetch.log')
        $sw.Stop()
        if ($LASTEXITCODE -ne 0) {
            Write-Warn2 ("fetch 退出码 {0}，已耗时 {1:N0} 分钟" -f $LASTEXITCODE, $sw.Elapsed.TotalMinutes)
            Write-Info 'gclient 支持断点续传，重跑 -Stage fetch 只补缺失部分。'
            Write-Info '若因并发过高中断，进 src 手动降并发: gclient sync -j 4 --nohooks --no-history'
            throw "fetch 失败 ($LASTEXITCODE)，看 logs\fetch.log"
        }
        Write-Info ("耗时 {0:N0} 分钟" -f $sw.Elapsed.TotalMinutes)
    } finally { Pop-Location }

    $sz = (Get-ChildItem (Join-Path $SrcRoot 'src') -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    Write-Ok ("源码拉取完成，src = {0:N1} GB" -f ($sz/1GB))
}

# ---------------------------------------------------------------- env
<#
    所有走 depot_tools 的阶段都必须先过这里。三个坑：
      1. DEPOT_TOOLS_UPDATE=0  —— 否则 gclient.bat 会先跑 update_depot_tools.bat，
         它内部会 git fetch/checkout 覆盖 depot_tools，并触发 bootstrap 的 git 探测。
         本机的 git 是 PortableGit（只有 mingw64 布局），新版 bootstrap.py 的
         search_win_git_directory 认不出来，直接 "Git was not found in PATH" 退出。
         我们已经手工补好了 python3.bat + python3_bin_reldir.txt，不需要它再自举。
      2. 代理环境变量 —— 机器级变量对已启动进程无效，必须灌进当前会话，
         否则 CIPD / vpython / curl 下载会走直连然后失败。
      3. DEPOT_TOOLS_WIN_TOOLCHAIN=0 —— 用本地 VS，不要去找 Google 内部工具链。
      4. vs2026_install —— vs_toolchain.py 的 _GenerateCandidatePaths 只认两种来源：
         环境变量 vs2026_install / vs2022_install ...，或硬编码的
         %ProgramFiles%\Microsoft Visual Studio\18\<Edition>。我们的 VS 在 E 盘，
         只能靠环境变量。而且必须写进「当前进程」：机器级变量虽然在注册表里，
         但本会话进程拿不到（DSH 每次起 pwsh 用的是启动时的旧快照），
         表现为 gn gen 报 "No supported Visual Studio can be found"。
#>
function Initialize-DepotEnv {
    $env:Path = "$DepotTools;$env:Path"
    $env:DEPOT_TOOLS_WIN_TOOLCHAIN = '0'
    $env:DEPOT_TOOLS_UPDATE = '0'

    if (-not $env:vs2026_install) {
        $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
        if (Test-Path $vswhere) {
            $p = & $vswhere -products * -version '[18.0,19.0)' -property installationPath 2>$null | Select-Object -First 1
            if ($p) { $env:vs2026_install = $p.Trim() }
        }
        if (-not $env:vs2026_install -and (Test-Path $InstallRoot)) { $env:vs2026_install = $InstallRoot }
    }

    # WINDOWSSDKDIR 同理：vs_toolchain.py 的 SetEnvironmentAndGetSDKDir 只在变量缺失时
    # 去硬编码的 %ProgramFiles(x86)%\Windows Kits\10 兜底，找不到就 KeyError。
    # 我们的 SDK 被 --path shared 放到了 E 盘，必须显式给。
    if (-not $env:WINDOWSSDKDIR) {
        $kits = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows Kits\Installed Roots' -Name KitsRoot10 -ErrorAction SilentlyContinue).KitsRoot10
        if (-not $kits) {
            $kits = (Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows Kits\Installed Roots' -Name KitsRoot10 -ErrorAction SilentlyContinue).KitsRoot10
        }
        if ($kits) { $env:WINDOWSSDKDIR = $kits.TrimEnd('\') }
    }

    if ($NetworkMode -eq 'proxy' -and $Proxy) {
        foreach ($v in @('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy')) {
            Set-Item -Path "env:$v" -Value $Proxy -ErrorAction SilentlyContinue
        }
    }
}

# ---------------------------------------------------------------- hooks
function Invoke-Hooks {
    Write-Step 'Stage: hooks — gclient runhooks（下载 clang / sysroot / 工具链）'

    Initialize-DepotEnv
    $src = Join-Path $SrcRoot 'src'
    if (-not (Test-Path $src)) { throw "源码不存在: $src，先跑 -Stage fetch" }

    Push-Location $src
    try {
        Write-Info 'gclient runhooks 会下载 clang、Windows sysroot、测试数据（约 3-6GB）'
        & cmd /c "gclient runhooks 2>&1" | Tee-Object -FilePath (Join-Path $script:LogDir 'hooks.log')
        if ($LASTEXITCODE -ne 0) { throw "runhooks 失败 ($LASTEXITCODE)，看 logs\hooks.log" }
    } finally { Pop-Location }

    # 验证 clang 真的落地了，runhooks 有时会「成功但没下完」
    $clangExe = Join-Path $src 'third_party\llvm-build\Release+Asserts\bin\clang-cl.exe'
    if (Test-Path $clangExe) {
        Write-Ok "clang-cl: $clangExe"
    } else {
        Write-Warn2 "clang-cl 未出现：$clangExe — 编译阶段会再拉一次，留意日志"
    }
    Write-Ok 'hooks 完成'
}

# ---------------------------------------------------------------- gen
function Invoke-Gen {
    Write-Step "Stage: gen — gn gen out\Release (BuildType=$BuildType)"

    Initialize-DepotEnv
    $src = Join-Path $SrcRoot 'src'
    if (-not (Test-Path (Join-Path $src 'buildtools'))) { throw "buildtools 缺失，先跑 -Stage hooks" }

    $gnArgs = switch ($BuildType) {
        'component' { 'is_debug=false is_component_build=true is_official_build=false symbol_level=1' }
        'official'  { 'is_debug=false is_component_build=false is_official_build=true symbol_level=0 use_thin_lto=false' }
        default     { 'is_debug=false is_component_build=false is_official_build=false symbol_level=1' }
    }

    # Public video sites require actual H.264/AAC decoders and MSE support.
    # This selects the FFmpeg codec set, without changing the product branding.
    $gnArgs += ' proprietary_codecs=true ffmpeg_branding="Chrome"'
    Push-Location $src
    try {
        Write-Info "gn args: $gnArgs"
        # Write arguments directly: nested cmd.exe quoting strips the quotes
        # required by the ffmpeg_branding GN string.
        $releaseDir = Join-Path $src 'out\Release'
        New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
        [IO.File]::WriteAllText((Join-Path $releaseDir 'args.gn'), (($gnArgs -split ' ') -join "`n"))
        & cmd /c "gn gen out\Release 2>&1" | Tee-Object -FilePath (Join-Path $script:LogDir 'gn.log')
        if ($LASTEXITCODE -ne 0) { throw "gn gen 失败 ($LASTEXITCODE)" }
    } finally { Pop-Location }

    Write-Ok "已生成 $src\out\Release"
}

# ---------------------------------------------------------------- build
function Invoke-Build {
    Write-Step 'Stage: build — autoninja 编译 chrome'

    Initialize-DepotEnv
    $src = Join-Path $SrcRoot 'src'
    if (-not (Test-Path (Join-Path $src 'out\Release\build.ninja'))) { throw "out\Release 未生成，先跑 -Stage gen" }

    # 8 物理核 16 线程：autoninja 默认按 逻辑核+2 起并发，链接阶段会顶内存
    # 32GB 下单进程链接峰值 15-26GB，编译并发压到 12 更稳
    $env:NINJA_SUMMARIZE_BUILD = '1'
    $env:GYP_MSVS_OVERRIDE_PATH = ''

    Push-Location $src
    try {
        Write-Info 'autoninja -C out\Release -j 12 chrome'
        $sw = [Diagnostics.Stopwatch]::StartNew()
        & cmd /c "autoninja -C out\Release -j 12 chrome 2>&1" | Tee-Object -FilePath (Join-Path $script:LogDir 'build.log')
        $sw.Stop()
        if ($LASTEXITCODE -ne 0) { throw "编译失败 ($LASTEXITCODE)，看 logs\build.log" }
        Write-Ok ("编译完成，耗时 {0:N1} 分钟" -f $sw.Elapsed.TotalMinutes)
    } finally { Pop-Location }

    $exe = Join-Path $src 'out\Release\chrome.exe'
    if (Test-Path $exe) {
        $sz = [math]::Round((Get-Item $exe).Length/1MB, 1)
        Write-Ok "产物: $exe ($sz MB)"
    } else {
        Write-Warn2 'chrome.exe 未找到，检查 out\Release'
    }
}

# ---------------------------------------------------------------- main
Write-Host @"

Chromium Build Setup
  Stage     : $Stage
  BuildType : $BuildType
  Network   : $NetworkMode
  SrcRoot   : $SrcRoot
  DepotTools: $DepotTools
  VS install: $InstallRoot
  VS cache  : $CacheRoot
  Proxy     : $Proxy

"@ -ForegroundColor White

$order = @('prereq','proxy','vs','sdk','deptools','fetch','hooks','gen','build')
$run = if ($Stage -eq 'all') { $order } else { @($Stage) }

foreach ($s in $run) {
    switch ($s) {
        'prereq'   { Invoke-Prereq }
        'proxy'    { Invoke-ProxySetup }
        'vs'       { Invoke-Vs }
        'sdk'      { Invoke-Sdk }
        'deptools' { Invoke-DepotTools }
        'fetch'    { Invoke-Fetch }
        'hooks'    { Invoke-Hooks }
        'gen'      { Invoke-Gen }
        'build'    { Invoke-Build }
    }
}

Write-Host "`n全部完成。" -ForegroundColor Green
Write-Host "若有阶段要求重启，重启后从断点继续：.\setup-chromium-build.ps1 -Stage <阶段名>" -ForegroundColor Gray
