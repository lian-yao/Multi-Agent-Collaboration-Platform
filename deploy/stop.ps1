<#
.SYNOPSIS
    停止平台。**不带参数 = 停本地服务形态**（与 `start.ps1` 对称）。

.DESCRIPTION
    启动与停止必须对称：只有启动收敛成一个入口、停止还散在别处，早晚会有一处漏改。

      .\stop.ps1              # 本地服务形态：停三个宿主进程 + 它起的依赖容器
      .\stop.ps1 -Container   # 容器形态：docker compose down（与既有行为一致）

    本地形态只 `stop` 依赖容器、不 `down`：容器与数据都留着，下次 `start.ps1` 起得更快；
    要连容器一起删就用 `-Container` 或自己 `docker compose down`。
#>
[CmdletBinding()]
param(
    [switch]$Container
)

$ErrorActionPreference = "Stop"

$composeFile = Join-Path $PSScriptRoot "compose.yaml"
$localScript = Join-Path (Split-Path $PSScriptRoot -Parent) "scripts\start_local.ps1"

if (-not $Container) {
    if (-not (Test-Path $localScript)) {
        Write-Error "找不到 $localScript —— 默认形态（本地服务）需要它。要停容器形态请加 -Container。"
    }
    Write-Host "默认形态：本地服务（ADR-035）。要停容器形态请加 -Container。" -ForegroundColor Yellow
    # 三个宿主进程（含子进程树）+ 它起的依赖容器。依赖用 `stop` 不用 `down`：容器与数据保留。
    # 内层用的是 `Write-Host`（信息流），只重定向 `2>&1` 是抓不到的——要 `*>&1`。
    $output = & $localScript -Stop *>&1
    foreach ($line in $output) {
        # 内层脚本会提示"依赖容器仍在运行"——紧接着我们就停它们，那句话在这里会自相矛盾。
        if ("$line" -notmatch '依赖容器') { Write-Host "$line" }
    }
    if (Test-Path $composeFile) {
        # `docker compose stop` 把进度写到 stderr；Windows PowerShell 5.1 会把它渲染成红字错误，
        # 看起来像"停止失败"。用仓库里既有的处理方式：吞掉全部流，再按退出码判定。
        $previous = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        docker compose -f $composeFile stop redis postgres jaeger *> $null
        $stopExit = $LASTEXITCODE
        $ErrorActionPreference = $previous
        if ($stopExit -ne 0) {
            Write-Warning "依赖容器没能全部停掉（docker compose stop 退出码 $stopExit）；可手动执行：cd deploy; docker compose stop redis postgres jaeger"
        }
    }
    Write-Host "依赖容器已停（容器与数据保留）。要连容器一起删：.\stop.ps1 -Container" -ForegroundColor Yellow
    exit 0
}

Write-Host "形态：容器（deploy/compose.yaml）。" -ForegroundColor Yellow

if (-not (Test-Path $composeFile)) {
    Write-Error "Compose file not found: $composeFile"
}

# `docker compose down` 会把停止与移除进度写到 stderr。Windows PowerShell 5.1 在
# $ErrorActionPreference = "Stop" 下把原生命令的 stderr 当成终止性错误，于是容器确实
# 停掉了，脚本却以非零码退出、也没有任何提示——上游 `start.ps1; .\stop.ps1` 这样的
# 串跑会把成功当失败。与 `start.ps1` 的处理保持一致，临时切到 Continue 并用退出码判定成败。
$ErrorActionPreference = "Continue"
docker compose -f $composeFile down
$downExitCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($downExitCode -ne 0) {
    Write-Error "Docker Compose failed to stop services."
}

Write-Host "Services stopped."
