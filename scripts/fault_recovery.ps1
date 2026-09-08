$ErrorActionPreference = "Stop"

$composeFile = Join-Path $PSScriptRoot "..\deploy\compose.yaml"
$holdSeconds = 25

if (-not (Test-Path $composeFile)) {
    Write-Error "Compose file not found: $composeFile"
}

Write-Host "Scheduling workflow with hold=$holdSeconds seconds..."
$scheduleOutput = docker compose -f $composeFile exec -T backend uv run --no-sync python -m app.workflows.poc --hold-seconds $holdSeconds --no-wait
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to schedule workflow. Output: $scheduleOutput"
}

$match = [regex]::Match(($scheduleOutput -join "`n"), 'workflow_id=([0-9a-fA-F-]{36})')
if (-not $match.Success) {
    Write-Error "Could not parse workflow_id from output: $scheduleOutput"
}
$workflowId = $match.Groups[1].Value
Write-Host "Workflow scheduled: $workflowId"

Start-Sleep -Seconds 3
Write-Host "Killing backend process..."
docker compose -f $composeFile restart backend
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to restart backend."
}

Write-Host "Waiting for backend and Dapr sidecar to recover..."
docker compose -f $composeFile up -d --wait --wait-timeout 180
if ($LASTEXITCODE -ne 0) {
    Write-Error "Backend did not recover."
}
Start-Sleep -Seconds 5

Write-Host "Waiting for workflow to resume and complete..."
$result = docker compose -f $composeFile exec -T backend uv run --no-sync python -m app.workflows.poc --workflow-id $workflowId
if ($LASTEXITCODE -ne 0) {
    Write-Error "Workflow did not complete after restart. Output: $result"
}

Write-Host ""
Write-Host "Recovery verified: workflow_id=$workflowId"
Write-Host $result