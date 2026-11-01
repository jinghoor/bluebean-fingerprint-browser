<#
.SYNOPSIS
    管理这个项目对 Chromium 源码的改动（导出 / 查看 / 恢复）。

.DESCRIPTION
    改动以两种形式存在：

      patches\0001-fingerprint-browser.patch   对已有文件的修改（git diff）
      patches\new-files\...                    新增文件，保持目录结构

    合起来能把一份干净的 Chromium 158.0.8122.0 变成这个构建。

    为什么这样存：Chromium 源码 61 GB，项目本身只有 1 MB 多。把改动抽出来，
    项目可以随便备份、对比、搬到别的机器，而源码留在原地不用重编。

.PARAMETER Action
    export   从当前 chromium 导出最新 diff（改完源码后跑）
    status   看当前源码和 patch 有没有偏差
    list     列出 patch 和新增文件
    restore  把 patch + 新增文件写回源码（危险：会覆盖当前修改）

.EXAMPLE
    .\patches\manage.ps1 -Action status
    .\patches\manage.ps1 -Action export
#>
[CmdletBinding()]
param(
    [ValidateSet('export','status','list','restore')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Continue'
$projectRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $projectRoot 'chromium-src.json'
if (-not (Test-Path $configPath)) {
    $configPath = Join-Path $projectRoot 'chromium-src.example.json'
}
$cfg = Get-Content $configPath -Raw | ConvertFrom-Json
$source = $cfg.source
$patchDir = $PSScriptRoot
$patchFile = Join-Path $patchDir '0001-fingerprint-browser.patch'
$newFilesDir = Join-Path $patchDir 'new-files'

switch ($Action) {

'list' {
    Write-Host "===== 对已有文件的修改 =====" -ForegroundColor Cyan
    if (Test-Path $patchFile) {
        $sz = [math]::Round((Get-Item $patchFile).Length / 1KB, 1)
        Write-Host "  $patchFile  ($sz KB)"
        Get-Content $patchFile |
            Select-String -Pattern '^diff --git' |
            ForEach-Object { Write-Host "    $($_.Line -replace '^diff --git a/(\S+).*','$1')" }
    }
    Write-Host ""
    Write-Host "===== 新增文件 =====" -ForegroundColor Cyan
    Get-ChildItem $newFilesDir -Recurse -File |
        ForEach-Object { Write-Host "  $($_.FullName.Substring($newFilesDir.Length + 1))" }
}

'status' {
    Push-Location $source
    try {
        # 让 git 直接写临时文件：Out-String 会按控制台宽度折行，长行被硬切。
        $tmp = Join-Path $env:TEMP ("cur-{0}.patch" -f [guid]::NewGuid().ToString('N'))
        & cmd /c "git diff HEAD -- . > `"$tmp`" 2>nul"

        if (Test-Path $patchFile) {
            $cur = if (Test-Path $tmp) { Get-Content $tmp -Raw } else { '' }
            $saved = Get-Content $patchFile -Raw
            if ($cur -eq $saved) {
                Write-Host "  源码修改与 patch 一致" -ForegroundColor Green
            } else {
                Write-Host "  源码与 patch 不一致 —— 改完源码记得跑 -Action export" -ForegroundColor Yellow
                $stat = & git diff --stat HEAD -- . 2>$null | Select-Object -Last 1
                Write-Host "    当前: $stat"
                Write-Host "    patch: $((Get-Item $patchFile).LastWriteTime)"
            }
        } else {
            Write-Host "  patch 文件不存在，先跑 -Action export" -ForegroundColor Yellow
        }
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    } finally { Pop-Location }

    # 新增文件是否齐全
    $missing = @()
    Get-ChildItem $newFilesDir -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($newFilesDir.Length + 1)
        if (-not (Test-Path (Join-Path $source $rel))) { $missing += $rel }
    }
    if ($missing.Count -eq 0) {
        Write-Host "  新增文件齐全" -ForegroundColor Green
    } else {
        Write-Host "  源码里缺 $($missing.Count) 个新增文件:" -ForegroundColor Yellow
        $missing | ForEach-Object { Write-Host "    $_" }
    }
}

'export' {
    Push-Location $source
    try {
        $stat = & git diff --stat HEAD -- . 2>$null | Select-Object -Last 1
        Write-Host "  当前改动: $stat"
        & cmd /c "git diff HEAD -- . > `"$patchFile`" 2>nul"
        $kb = [math]::Round((Get-Item $patchFile).Length / 1KB, 1)
        Write-Host "  已写入 $patchFile  ($kb KB)" -ForegroundColor Green

        # 新增文件同步（排除备份、out 目录）
        $newFiles = & git ls-files --others --exclude-standard -- . 2>$null |
                    Where-Object { $_ -notmatch '\.(bak|orig|rej)(_|$)' -and $_ -notmatch '^out/' }
        foreach ($f in $newFiles) {
            $dst = Join-Path $newFilesDir $f
            $dir = Split-Path $dst -Parent
            if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
            Copy-Item (Join-Path $source $f) $dst -Force
        }
        Write-Host "  新增文件已同步: $($newFiles.Count) 个" -ForegroundColor Green
    } finally { Pop-Location }
}

'restore' {
    Write-Host "这会把 $patchFile 打进 $source" -ForegroundColor Yellow
    Write-Host "并覆盖 new-files 下的 $((Get-ChildItem $newFilesDir -Recurse -File).Count) 个文件。" -ForegroundColor Yellow
    $ans = Read-Host "继续？(yes/no)"
    if ($ans -ne 'yes') { Write-Host "取消"; exit 0 }

    Push-Location $source
    try {
        & git apply --verbose $patchFile 2>&1 | Select-Object -Last 5 | ForEach-Object { Write-Host "  $_" }
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  patch 应用失败 —— 源码可能不是干净的 158.0.8122.0" -ForegroundColor Red
            exit 1
        }
    } finally { Pop-Location }

    Get-ChildItem $newFilesDir -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($newFilesDir.Length + 1)
        $dst = Join-Path $source $rel
        $dir = Split-Path $dst -Parent
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Copy-Item $_.FullName $dst -Force
    }
    Write-Host "  恢复完成。接着跑 .\build\build.ps1" -ForegroundColor Green
}

}
