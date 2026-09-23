<#
.SYNOPSIS
    一键把平台按**本地服务形态**跑起来（ADR-035）：后端直跑在宿主上，浏览器只经 HTTP 拿结果。

.DESCRIPTION
    这条路上的四件事，本脚本一次做完：

    1. 起基础设施：`docker compose up -d redis postgres`（端口 6380 / 5433，与
       `app/core/storage.py` 的默认值对齐，不需要 .env）；
    2. 腾出端口：容器里的 backend / dapr-sidecar / frontend 占着 8000、3500、5173，
       本地模式要先把它们停掉（只 stop，不删容器与数据）；
    3. 起本地 Dapr sidecar（`scripts/run_local_sidecar.ps1` 的同一套参数）；
    4. 起后端（`app.workflows.worker`，绑 127.0.0.1）与前端（Vite dev，5173 → 反代 /api）。

    为什么默认形态是本地直跑：工作区的授权单位是**用户当场选定的一个文件夹**，
    只有后端跑在宿主上，它看到的路径才是宿主路径（ADR-035）。容器形态仍在，用
    `deploy/start.ps1`，并在 compose 里显式声明 `WORKSPACE_SOURCE=container`。

    绑定地址刻意收成 127.0.0.1：宿主目录浏览是「读整机目录结构」的能力，不能发布出去。

.PARAMETER Stop
    停掉本脚本起的那三个进程（daprd / 后端 / 前端），基础设施容器保持运行。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts/start_local.ps1
    powershell -ExecutionPolicy Bypass -File scripts/start_local.ps1 -Stop

.NOTES
    本文件必须保存为 **UTF-8 with BOM**：Windows PowerShell 5.1 对没有 BOM 的脚本按
    ANSI(GBK) 读，脚本里的中文**字符串**会错位到吃掉引号，直接变成语法错误
    （`pick_work_dir.ps1` 头部记过同一个坑）。
#>
[CmdletBinding()]
param(
    [switch]$Stop
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $projectRoot "deploy\compose.yaml"
$stateFile = Join-Path $env:TEMP "macp-local-run.json"

function Get-State {
    if (Test-Path $stateFile) {
        try {
            return Get-Content -Path $stateFile -Raw | ConvertFrom-Json
        } catch {
            return $null
        }
    }
    return $null
}

function Stop-StartedProcess {
    param([string]$Name, [int]$Id)

    $process = Get-Process -Id $Id -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Write-Host "  $Name 的包装进程已不在（子进程可能还在，见下）"
    }
    # **必须连子进程一起杀**：记录下来的 PID 是包装进程（powershell / cmd），
    # 真正干活的是它们的子进程——`daprd.exe` 与 vite 的 `node.exe`。只杀父进程会留下
    # 孤儿占着 3500 / 5173，下一次启动就撞端口。`/T` 杀整棵进程树。
    # 仍然只按 PID 杀自己记录的那几个，绝不按名字杀（用户可能自己开着 daprd / node）。
    $ErrorActionPreference = "Continue"
    taskkill /PID $Id /T /F *> $null
    Write-Host "  已停止 $Name (PID $Id 及其子进程)"
}

if ($Stop) {
    $state = Get-State
    if ($null -eq $state) {
        Write-Host "没有找到 $stateFile —— 没有本脚本记录过的进程。"
        exit 0
    }
    Write-Host "停止本地服务形态的三个进程："
    foreach ($entry in @(
        @{ Name = "前端（Vite）"; Id = $state.frontend },
        @{ Name = "后端（worker）"; Id = $state.backend },
        @{ Name = "Dapr sidecar"; Id = $state.daprd }
    )) {
        if ($entry.Id) { Stop-StartedProcess -Name $entry.Name -Id ([int]$entry.Id) }
    }
    Remove-Item -Path $stateFile -Force -ErrorAction SilentlyContinue
    Write-Host ""
    Write-Host "基础设施容器（redis / postgres）仍在运行；要一并停掉：cd deploy; docker compose stop redis postgres" -ForegroundColor Yellow
    exit 0
}

foreach ($command in @("docker", "uv", "node")) {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        Write-Error "缺少命令：$command"
    }
}
if (-not (Test-Path $composeFile)) {
    Write-Error "找不到 $composeFile"
}

$existing = Get-State
if ($null -ne $existing) {
    $alive = @($existing.frontend, $existing.backend, $existing.daprd) |
        Where-Object { $_ -and (Get-Process -Id ([int]$_) -ErrorAction SilentlyContinue) }
    if ($alive.Count -gt 0) {
        Write-Host "已有本地进程在跑（PID $($alive -join ', ')）。先执行 -Stop 再启动。" -ForegroundColor Yellow
        exit 1
    }
}

Write-Host "[1/4] 起基础设施（Redis / PostgreSQL）" -ForegroundColor Green
docker compose -f $composeFile up -d redis postgres

Write-Host "[2/4] 腾出 8000 / 3500 / 5173：停掉容器里的 backend / dapr-sidecar / frontend" -ForegroundColor Green
$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"  # 容器没起过时 stop 会报错，这不是失败
docker compose -f $composeFile stop backend dapr-sidecar frontend | Out-Null
$ErrorActionPreference = $previous

$daprdLog = Join-Path $env:TEMP "macp-local-daprd.log"
$backendLog = Join-Path $env:TEMP "macp-local-backend.log"
$frontendLog = Join-Path $env:TEMP "macp-local-frontend.log"

Write-Host "[3/4] 起本地 Dapr sidecar 与后端（绑 127.0.0.1）" -ForegroundColor Green
# 后台常驻进程一律隐藏窗口：它们是服务，用户交互发生在浏览器里。
$daprdArgs = "-ExecutionPolicy Bypass -NoProfile -File `"$(Join-Path $PSScriptRoot 'run_local_sidecar.ps1')`""
$daprd = Start-Process -FilePath "powershell.exe" -ArgumentList $daprdArgs `
    -WorkingDirectory $projectRoot -WindowStyle Hidden `
    -RedirectStandardOutput $daprdLog -RedirectStandardError "$daprdLog.err" -PassThru

# 形态必须显式写出来：虽然代码默认就是 host，但这份脚本是"默认形态"的定义处，
# 让人一眼看到它跑的是哪一套语义（ADR-035 §2）。
$backendEnv = @{
    "WORKSPACE_SOURCE" = "host"
    "HOST"             = "127.0.0.1"
    "PORT"             = "8000"
    "PYTHONUNBUFFERED" = "1"
}
# 直接用项目 venv，**不要**让 `uv run` 去重新解析依赖：本机镜像对部分包返 403，
# 一次 `uv run`（默认会 sync）就会以 "requirements are unsatisfiable" 收场，
# 而后端根本没启动。容器里同理用的是 `uv run --no-sync`；这里更进一步，连 uv 都不经过。
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $backendExe = $venvPython
    $backendArgs = @("-m", "app.workflows.worker")
} else {
    Write-Warning "没有找到 $venvPython，退回 `uv run --no-sync`（若仍失败请先跑 uv sync）"
    $backendExe = "uv"
    $backendArgs = @("run", "--no-sync", "python", "-m", "app.workflows.worker")
}
foreach ($key in $backendEnv.Keys) { Set-Item -Path "Env:$key" -Value $backendEnv[$key] }
$backend = Start-Process -FilePath $backendExe -ArgumentList $backendArgs `
    -WorkingDirectory $projectRoot -WindowStyle Hidden `
    -RedirectStandardOutput $backendLog -RedirectStandardError "$backendLog.err" -PassThru

$deadline = (Get-Date).AddSeconds(90)
$healthy = $false
while ((Get-Date) -lt $deadline) {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 5
        if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) { $healthy = $true; break }
    } catch {
        # 还在启动；继续等到 deadline
    }
    Start-Sleep -Seconds 2
}
if (-not $healthy) {
    Write-Warning "后端 90 秒内没有就绪，看日志：$backendLog / $backendLog.err"
} else {
    Write-Host "  后端就绪：http://127.0.0.1:8000"
}

Write-Host "[4/4] 起前端开发服务器（5173 → 反代 /api 到 :8000）" -ForegroundColor Green
$frontend = Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", "npm run dev") `
    -WorkingDirectory (Join-Path $projectRoot "frontend") -WindowStyle Hidden `
    -RedirectStandardOutput $frontendLog -RedirectStandardError "$frontendLog.err" -PassThru

$deadline = (Get-Date).AddSeconds(60)
$uiReady = $false
while ((Get-Date) -lt $deadline) {
    try {
        $response = Invoke-WebRequest -Uri "http://localhost:5173/" -UseBasicParsing -TimeoutSec 5
        if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) { $uiReady = $true; break }
    } catch {
        Start-Sleep -Seconds 2
    }
    Start-Sleep -Milliseconds 500
}

@{ frontend = $frontend.Id; backend = $backend.Id; daprd = $daprd.Id } |
    ConvertTo-Json | Set-Content -Path $stateFile -Encoding UTF8

Write-Host ""
if ($uiReady) {
    Write-Host "Web UI:      http://localhost:5173" -ForegroundColor Green
} else {
    Write-Warning "前端 60 秒内没就绪，看日志：$frontendLog"
}
Write-Host "后端（本机）: http://127.0.0.1:8000   （只绑回环，未发布到网络）"
Write-Host "日志:        $backendLog / $frontendLog / $daprdLog"
Write-Host "停止:        powershell -ExecutionPolicy Bypass -File scripts/start_local.ps1 -Stop"
Write-Host ""
Write-Host "工作区形态：host —— 在工作台点「工作区」，选择文件夹时浏览的是**本机磁盘**。" -ForegroundColor Yellow
