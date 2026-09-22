$ErrorActionPreference = "Stop"

$composeFile = Join-Path $PSScriptRoot "compose.yaml"

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
