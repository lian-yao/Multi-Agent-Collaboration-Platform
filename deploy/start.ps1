<#
.SYNOPSIS
    启动平台。**不带参数 = 本地服务形态**（ADR-035 的默认形态）。

.DESCRIPTION
    两种形态共用 3500 / 8000 / 5173 三个端口，不可能同时跑，所以入口只有一个、
    用开关选形态：

      .\start.ps1              # 本地服务形态：宿主后端（绑 127.0.0.1）+ Vite dev + 依赖容器
      .\start.ps1 -Container   # 容器形态：backend / dapr-sidecar / frontend 全在 compose 里

    为什么默认是本地服务形态：工作区的授权单位是"用户当场选定的一个文件夹"，只有后端跑在宿主上，
    它看到的路径才是宿主路径（浏览器给不了宿主路径，bind mount 又在容器创建时固定）。这条默认
    写在 README 与 `doc/deployment.md` 里，这里把它落到**同一个入口**上——否则"默认形态"只是
    文档里的一句话，实际还得记住另一个脚本名。

    不带参数时这里只做一件事：把参数交给 `scripts/start_local.ps1`（它负责停掉容器里那三个
    占端口的服务、起依赖、起 sidecar 与后端、起前端）。容器形态的既有逻辑原样保留在下面。

.PARAMETER Container
    走容器形态（部署 / 演示用）。与本地服务形态互斥：切过去之前请先
    `scripts/start_local.ps1 -Stop`，或直接跑不带参数的 `.\start.ps1` 让它反过来切。

.PARAMETER Stop
    停止本地服务形态起的三个进程（宿主后端 / Vite / 本地 sidecar）。容器形态用
    `docker compose stop`（或 `deploy\stop.ps1`）。
#>
[CmdletBinding()]
param(
    [switch]$Container,
    [switch]$Stop
)

$ErrorActionPreference = "Stop"

$composeFile = Join-Path $PSScriptRoot "compose.yaml"

$localScript = Join-Path (Split-Path $PSScriptRoot -Parent) "scripts\start_local.ps1"

if ((-not $Container) -and (-not (Test-Path $localScript))) {
    Write-Error "找不到 $localScript —— 默认形态（本地服务）需要它。要跑容器形态请加 -Container。"
}

if ($Stop) {
    # 停止只对本地服务形态有意义：那三个进程是本仓库起的宿主进程；容器那套由 compose 管。
    if ($Container) {
        Write-Host "容器形态请用：docker compose stop（或 deploy\stop.ps1）" -ForegroundColor Yellow
        exit 0
    }
    & $localScript -Stop
    exit $LASTEXITCODE
}

if (-not $Container) {
    Write-Host "默认形态：本地服务（ADR-035）。要跑容器形态请加 -Container。" -ForegroundColor Yellow
    & $localScript
    exit $LASTEXITCODE
}

Write-Host "形态：容器（deploy/compose.yaml）。本地服务形态请运行不带参数的 .\start.ps1。" -ForegroundColor Yellow

# 切到容器形态前**先把本地服务形态收掉**：两套共用 3500 / 8000 / 5173，不收就会撞端口
# （"bind: Only one usage of each socket address" 那个报错就是这么来的）。反过来切由
# start_local.ps1 自己负责停容器里的那三个服务——两个方向都不要求使用者手工腾端口。
if (Test-Path $localScript) {
    $null = & $localScript -Stop 2>&1
    Write-Host "已确保本地服务形态的进程停干净（若原本在跑）。"
}

function Assert-CommandAvailable {
    param([Parameter(Mandatory = $true)][string]$Name)

    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        Write-Error "Required command not found: $Name"
    }
}

function Wait-HttpHealth {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Url,
        [int]$TimeoutSeconds = 60
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) {
                Write-Host "$Name is healthy."
                return
            }
        }
        catch {
            # Service may still be starting; keep polling until the deadline.
        }
        Start-Sleep -Seconds 2
    }

    Write-Error "$Name did not become healthy within $TimeoutSeconds seconds: $Url"
}

if (-not (Test-Path $composeFile)) {
    Write-Error "Compose file not found: $composeFile"
}

Assert-CommandAvailable "docker"

$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
docker info *> $null
$dockerExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorActionPreference
if ($dockerExitCode -ne 0) {
    Write-Error "Docker Engine is not available. Start Docker Desktop, then run this script again."
}

$ErrorActionPreference = "Continue"
docker compose version *> $null
$composeExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorActionPreference
if ($composeExitCode -ne 0) {
    Write-Error "Docker Compose is not available."
}

$ErrorActionPreference = "Continue"
docker compose -f $composeFile config --quiet
$configExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorActionPreference
if ($configExitCode -ne 0) {
    Write-Error "Docker Compose configuration is invalid: $composeFile"
}

Write-Host "Starting frontend, backend, Dapr sidecar, Redis, PostgreSQL, Jaeger, and Prometheus..."
# docker compose 把构建与拉取进度写到 stderr。Windows PowerShell 5.1 在
# $ErrorActionPreference = "Stop" 下会把原生命令的 stderr 当成终止性错误，
# 结果镜像构建成功、容器也起来了，脚本却在这里中断：既不执行下面的健康检查，
# 也不打印访问地址，看起来像部署失败。与上面 docker info / compose version /
# compose config 的处理保持一致，临时切到 Continue 并用退出码判定成败。
$ErrorActionPreference = "Continue"
docker compose -f $composeFile up -d --build --wait --wait-timeout 600
$upExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorActionPreference
if ($upExitCode -ne 0) {
    Write-Error "Docker Compose failed to start services."
}

Wait-HttpHealth "Frontend" "http://localhost:5173/"
# backend 只接 internal 网络、不再发布 8000（ADR-034 §4 的网络层强制），
# 所以它的健康探针经前端反代走 —— nginx 里有一条 `location = /health`。
Wait-HttpHealth "Backend" "http://localhost:5173/health"
Wait-HttpHealth "Dapr Sidecar" "http://localhost:3500/v1.0/metadata"

Write-Host ""
Write-Host "Web UI:       http://localhost:5173"
Write-Host "Backend API:  http://localhost:5173/api/v1  (经前端反代；backend 不发布宿主端口)"
Write-Host "Dapr API:     http://localhost:3500"
Write-Host "Jaeger:       http://localhost:16686"
Write-Host "Prometheus:   http://localhost:9090"
Write-Host "Redis:        localhost:6380"
Write-Host "PostgreSQL:   localhost:5433"
