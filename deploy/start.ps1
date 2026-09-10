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
docker compose -f $composeFile up -d --build --wait --wait-timeout 600
if ($LASTEXITCODE -ne 0) {
    Write-Error "Docker Compose failed to start services."
}

Wait-HttpHealth "Frontend" "http://localhost:5173/"
Wait-HttpHealth "Backend" "http://localhost:8000/health"
Wait-HttpHealth "Dapr Sidecar" "http://localhost:3500/v1.0/metadata"

Write-Host ""
Write-Host "Web UI:       http://localhost:5173"
Write-Host "Backend:      http://localhost:8000"
Write-Host "Dapr API:     http://localhost:3500"
Write-Host "Jaeger:       http://localhost:16686"
Write-Host "Prometheus:   http://localhost:9090"
Write-Host "Redis:        localhost:6380"
Write-Host "PostgreSQL:   localhost:5433"
