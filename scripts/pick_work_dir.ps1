<#
.SYNOPSIS
    在**宿主**上弹出原生文件夹选择框，把选中目录配成工作区根（WORKSPACE_HOST_ROOT）。

.DESCRIPTION
    浏览器里的「选择文件夹」按钮只能**导入副本**：浏览器拿不到宿主路径，只能把文件内容
    传上去（`doc/api.md` §7.1）。要让 Agent 直接操作你本机那个目录，宿主目录必须以 bind
    mount 的方式出现在 backend 容器里——这需要两步：写 `deploy/.env` 的
    `WORKSPACE_HOST_ROOT`，再重建 backend。本脚本把这两步包成一个动作。

    为什么不能由浏览器点击触发：`deploy/compose.yaml` 里的 backend 跑在容器里，
    容器的进程打不开宿主的原生对话框；能打开对话框的只有宿主上的进程。

    目录对话框用 `System.Windows.Forms.FolderBrowserDialog`（Windows 自带，不装依赖）。

    注意：**本文件必须保存为 UTF-8 with BOM**。Windows PowerShell 5.1 对没有 BOM 的脚本按
    ANSI(GBK) 读，脚本里的中文**字符串**会被错解、错位到吃掉引号，直接变成语法错误
    （`start.ps1` 不受影响是因为它的中文只在注释里）。用非 BOM 感知的编辑器改完记得补回来。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/pick_work_dir.ps1
    # 选完目录后按提示重建：cd deploy; docker compose up -d backend
    # 或者直接加 -Recreate 让脚本替你重建
#>
[CmdletBinding()]
param(
    [switch]$Recreate
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$deployDir = Join-Path $projectRoot "deploy"
$envFile = Join-Path $deployDir ".env"
$composeFile = Join-Path $deployDir "compose.yaml"

if (-not (Test-Path $composeFile)) {
    Write-Error "找不到 $composeFile —— 请在仓库里运行本脚本。"
}

Add-Type -AssemblyName System.Windows.Forms | Out-Null

$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = "选择 Agent 的工作文件夹（它会被挂进 backend 容器的 /workspace）"
$dialog.ShowNewFolderButton = $true
if (Test-Path $envFile) {
    # 已经配过就停在上次的根上，省得每次重新找。
    $existing = Select-String -Path $envFile -Pattern '^WORKSPACE_HOST_ROOT=(.+)$' -ErrorAction SilentlyContinue
    if ($existing) {
        $candidate = $existing.Matches[0].Groups[1].Value.Trim()
        if (Test-Path $candidate) { $dialog.SelectedPath = $candidate }
    }
}

$choice = $dialog.ShowDialog()
if ($choice -ne [System.Windows.Forms.DialogResult]::OK) {
    Write-Host "已取消，未改动配置。"
    exit 0
}

$selected = $dialog.SelectedPath
if (-not (Test-Path $selected)) {
    Write-Error "选中的目录不存在：$selected"
}
if ($selected.StartsWith($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    # 与 ADR-033 的禁令一致：仓库里是平台自己的源码，不能被当成 Agent 的工作区。
    Write-Error "不要选仓库目录或其子目录（$projectRoot）——阶段 2 起沙箱对它可写。请另选一个目录。"
}

$lines = @()
if (Test-Path $envFile) {
    $lines = Get-Content -Path $envFile | Where-Object { $_ -notmatch '^WORKSPACE_HOST_ROOT=' }
} else {
    $lines = @("# 由 scripts/pick_work_dir.ps1 生成；不要提交（.env 已在 .gitignore 中）。")
}
$lines += "WORKSPACE_HOST_ROOT=$selected"
Set-Content -Path $envFile -Value $lines -Encoding UTF8

Write-Host ""
Write-Host "工作区根已写入 $envFile ：" -ForegroundColor Green
Write-Host "  WORKSPACE_HOST_ROOT=$selected"
Write-Host ""
Write-Host "下一步（换根必须重建 backend，bind mount 在容器创建时就固定）：" -ForegroundColor Yellow
Write-Host "  cd `"$deployDir`"; docker compose up -d backend"
Write-Host ""

if ($Recreate) {
    Push-Location $deployDir
    try {
        docker compose up -d backend
    } finally {
        Pop-Location
    }
}
