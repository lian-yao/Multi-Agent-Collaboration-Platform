$ErrorActionPreference = "Stop"

$composeFile = Join-Path $PSScriptRoot "compose.yaml"

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
