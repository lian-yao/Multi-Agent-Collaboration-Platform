$ErrorActionPreference = "Stop"

$composeFile = Join-Path $PSScriptRoot "compose.yaml"

docker compose -f $composeFile up -d --build

Write-Host ""
Write-Host "Backend:      http://localhost:8000"
Write-Host "Dapr API:     http://localhost:3500"
Write-Host "Jaeger:       http://localhost:16686"
Write-Host "Prometheus:   http://localhost:9090"
Write-Host "Redis:        localhost:6380"
Write-Host "PostgreSQL:   localhost:5433"
