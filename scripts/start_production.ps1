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
    $Supervisor = Join-Path $PSScriptRoot "supervise_production.ps1"
    $Arguments = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $Supervisor,
        "-EnvironmentPath", $EnvironmentPath, "-RuntimePath", $RuntimePath,
        "-PermitPath", $PermitPath
    )
    if ($IgnoreUptimeProxy) { $Arguments += "-IgnoreUptimeProxy" }
}

$OutLog = Join-Path $RuntimePath "launcher.stdout.log"
$ErrorLog = Join-Path $RuntimePath "launcher.stderr.log"
$QuotedArguments = $Arguments | ForEach-Object {
    if ($_ -match '"') { throw "Argument contains an unsupported quote." }
    '"' + $_.TrimEnd('\') + '"'
}
$Executable = if ($Mode -eq "Live") { "powershell.exe" } else { $Python }
$Process = Start-Process -FilePath $Executable -ArgumentList ($QuotedArguments -join ' ') -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden -RedirectStandardOutput $OutLog -RedirectStandardError $ErrorLog -PassThru
Set-Content -LiteralPath (Join-Path $RuntimePath "process.id") -Value $Process.Id -Encoding ascii
Write-Output "Started $Mode process $($Process.Id). Live mode uses the Windows supervisor; inspect status files and logs."
