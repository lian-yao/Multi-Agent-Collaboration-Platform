# Start a local Dapr sidecar for running the backend on the host (PyCharm / CLI).
#
# Why this exists: the backend process needs a Dapr sidecar, but the one in
# deploy/compose.yaml uses --app-channel-address backend, so it only serves the
# containerized backend and is unreachable from a host process. This script runs
# daprd directly, pointed at the local component overrides.
#
# Prerequisites:
#   1) `dapr init` has been run (needs the dapr_placement / dapr_scheduler containers);
#   2) Redis and PostgreSQL are up:
#      docker compose -f deploy/compose.yaml up -d redis postgres
#   3) ports are free - stop the containerized backend and sidecar first:
#      docker compose -f deploy/compose.yaml stop backend dapr-sidecar
#
# Then run the "Backend" run configuration in PyCharm (module app.workflows.worker);
# the app defaults to localhost:3500 / localhost:50001.
#
# If the default ports are taken, override them, e.g.:
#   .\scripts\run_local_sidecar.ps1 -AppPort 8001 -DaprHttpPort 3501 -DaprGrpcPort 50002
# and set PORT / DAPR_HTTP_ENDPOINT / DAPR_GRPC_ENDPOINT for the app accordingly.

param(
    [int]$AppPort = 8000,
    [int]$DaprHttpPort = 3500,
    [int]$DaprGrpcPort = 50001,
    # Defaults to 9099 because daprd's default metrics port 9090 clashes with
    # the Prometheus container published by deploy/compose.yaml.
    [int]$MetricsPort = 9099
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path $PSScriptRoot -Parent
$daprd = Join-Path $env:USERPROFILE ".dapr\bin\daprd.exe"

if (-not (Test-Path $daprd)) {
    Write-Error "daprd not found: $daprd - run 'dapr init' first."
}

# Ports match deploy/compose.yaml (backend 8000, sidecar 3500/50001) so that the
# app's defaults in app/core/dapr.py work unchanged.
& $daprd `
    --app-id backend `
    --app-port $AppPort `
    --app-channel-address localhost `
    --dapr-http-port $DaprHttpPort `
    --dapr-grpc-port $DaprGrpcPort `
    --metrics-port $MetricsPort `
    --resources-path (Join-Path $projectRoot "deploy\dapr\components-local") `
    --placement-host-address localhost:6050 `
    --scheduler-host-address localhost:6060
