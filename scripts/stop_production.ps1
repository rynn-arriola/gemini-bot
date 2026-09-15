param([string]$RuntimeDirectory = "data\production")

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimePath = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $RuntimeDirectory))
$DataRoot = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot "data"))
if (-not $RuntimePath.StartsWith($DataRoot + [System.IO.Path]::DirectorySeparatorChar)) {
    throw "RuntimeDirectory must resolve below this project's data directory."
}
if (-not (Test-Path -LiteralPath $RuntimePath -PathType Container)) {
    throw "Runtime directory does not exist."
}
Set-Content -LiteralPath (Join-Path $RuntimePath "STOP") -Value "operator stop requested" -Encoding ascii
Set-Content -LiteralPath (Join-Path $RuntimePath "SUPERVISOR_STOP") -Value "operator stop requested" -Encoding ascii
Write-Output "Supervisor restart is disabled and graceful drain was requested. Confirm status.json reaches stopped_flat; do not kill the process first."
