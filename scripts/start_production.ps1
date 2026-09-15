param(
    [ValidateSet("Prepare", "Live")]
    [string]$Mode = "Prepare",
    [string]$EnvironmentFile = ".env",
    [string]$RuntimeDirectory = "data\production",
    [string]$ActivationPermit = "",
    [switch]$IgnoreUptimeProxy
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$EnvironmentPath = (Resolve-Path (Join-Path $ProjectRoot $EnvironmentFile)).Path
$RuntimePath = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $RuntimeDirectory))
$DataRoot = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot "data"))

if (-not $RuntimePath.StartsWith($DataRoot + [System.IO.Path]::DirectorySeparatorChar)) {
    throw "RuntimeDirectory must resolve below this project's data directory."
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project virtual-environment Python was not found."
}
New-Item -ItemType Directory -Path $RuntimePath -Force | Out-Null

$Arguments = @("-m", "gemini_bot")
if ($Mode -eq "Prepare") {
    $Arguments += @("prepare-live", "--env", $EnvironmentPath, "--directory", $RuntimePath)
} else {
    if ([string]::IsNullOrWhiteSpace($ActivationPermit)) {
        throw "Live mode requires an owner-approved activation permit path."
    }
    $PermitPath = (Resolve-Path (Join-Path $ProjectRoot $ActivationPermit)).Path
    $Arguments += @("run-live", "--env", $EnvironmentPath, "--directory", $RuntimePath,
                    "--activation-permit", $PermitPath)
    if ($IgnoreUptimeProxy) {
        $Arguments += "--ignore-uptime-proxy"
    }
}

$OutLog = Join-Path $RuntimePath "launcher.stdout.log"
$ErrorLog = Join-Path $RuntimePath "launcher.stderr.log"
$QuotedArguments = $Arguments | ForEach-Object {
    if ($_ -match '"') { throw "Argument contains an unsupported quote." }
    '"' + $_.TrimEnd('\') + '"'
}
$Process = Start-Process -FilePath $Python -ArgumentList ($QuotedArguments -join ' ') -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden -RedirectStandardOutput $OutLog -RedirectStandardError $ErrorLog -PassThru
Set-Content -LiteralPath (Join-Path $RuntimePath "process.id") -Value $Process.Id -Encoding ascii
Write-Output "Started $Mode process $($Process.Id). Inspect status.json and logs; this script does not grant approval."
