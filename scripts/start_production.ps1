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

if ($Mode -eq "Prepare") {
    $Process = Start-Process -FilePath $Python -ArgumentList ($QuotedArguments -join ' ') -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden -RedirectStandardOutput $OutLog -RedirectStandardError $ErrorLog -PassThru
    Set-Content -LiteralPath (Join-Path $RuntimePath "process.id") -Value $Process.Id -Encoding ascii
    Write-Output "Started Prepare process $($Process.Id); inspect status files and logs."
    exit 0
}

# A normal hidden child still belongs to the caller's Windows job. A terminal,
# automation host, or Codex command timeout can therefore terminate the entire
# bot tree even though the child has its own window. Launch Live through an
# on-demand Scheduled Task instead. Task Scheduler owns the supervisor process,
# and the task has no boot/logon trigger and no independent restart authority.
$SystemPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
if (-not (Test-Path -LiteralPath $SystemPowerShell -PathType Leaf)) {
    throw "System Windows PowerShell was not found."
}

$HashAlgorithm = [System.Security.Cryptography.SHA256]::Create()
try {
    $RootBytes = [System.Text.Encoding]::UTF8.GetBytes($ProjectRoot.ToLowerInvariant())
    $RootHash = [BitConverter]::ToString($HashAlgorithm.ComputeHash($RootBytes)).Replace("-", "").Substring(0, 12)
} finally {
    $HashAlgorithm.Dispose()
}
$TaskName = "GeminiBotProduction-$RootHash"
$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $ExistingTask -and [string]$ExistingTask.State -eq "Running") {
    throw "The persistent Gemini production task is already running; inspect runtime status instead of starting another owner."
}

$PreviousStatusPath = Join-Path $RuntimePath "supervisor-status.json"
if (Test-Path -LiteralPath $PreviousStatusPath -PathType Leaf) {
    $PreviousStatus = $null
    try {
        $PreviousStatus = Get-Content -LiteralPath $PreviousStatusPath -Raw | ConvertFrom-Json
    } catch {
        # An old malformed status is not liveness proof. The production runner
        # will still perform the authoritative ownership and account checks.
    }
    if ($null -ne $PreviousStatus -and $null -ne $PreviousStatus.supervisor_pid `
            -and $null -ne (Get-Process -Id ([int]$PreviousStatus.supervisor_pid) -ErrorAction SilentlyContinue)) {
        throw "A supervisor recorded for this runtime directory still exists; refusing a second mutation owner."
    }
}

$ActionArguments = @(
    "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", $Supervisor,
    "-EnvironmentPath", $EnvironmentPath, "-RuntimePath", $RuntimePath,
    "-PermitPath", $PermitPath
)
if ($IgnoreUptimeProxy) { $ActionArguments += "-IgnoreUptimeProxy" }
$QuotedActionArguments = $ActionArguments | ForEach-Object {
    if ($_ -match '"') { throw "Argument contains an unsupported quote." }
    '"' + $_.TrimEnd('\') + '"'
}

$Action = New-ScheduledTaskAction -Execute $SystemPowerShell `
    -Argument ($QuotedActionArguments -join ' ') -WorkingDirectory $ProjectRoot
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$Principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited
$Task = New-ScheduledTask -Action $Action -Settings $Settings -Principal $Principal
Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null

$StartedAt = [DateTimeOffset]::UtcNow
Start-ScheduledTask -TaskName $TaskName

$ConfirmedStatus = $null
$Deadline = [DateTimeOffset]::UtcNow.AddSeconds(30)
do {
    Start-Sleep -Milliseconds 250
    if (Test-Path -LiteralPath $PreviousStatusPath -PathType Leaf) {
        try {
            $CandidateStatus = Get-Content -LiteralPath $PreviousStatusPath -Raw | ConvertFrom-Json
            $CandidateUpdatedAt = [DateTimeOffset]::Parse([string]$CandidateStatus.updated_at)
            $CandidateProcess = Get-Process -Id ([int]$CandidateStatus.supervisor_pid) -ErrorAction SilentlyContinue
            if ($CandidateUpdatedAt -ge $StartedAt -and $null -ne $CandidateProcess `
                    -and $CandidateStatus.state -in @("STARTING", "RUNNING", "BACKOFF", "RECONCILING", "ATTENTION_REQUIRED")) {
                $ConfirmedStatus = $CandidateStatus
                break
            }
        } catch {
            # Atomic writers can briefly leave no readable destination during a
            # replace. Keep waiting for a fresh, process-backed heartbeat.
        }
    }
} while ([DateTimeOffset]::UtcNow -lt $Deadline)

$LauncherStatePath = Join-Path $RuntimePath "launcher-task.json"
$LauncherState = [ordered]@{
    task_name = $TaskName
    registered_at = $StartedAt.ToString("o")
    execution_time_limit = "PT0S"
    launch_kind = "on_demand_scheduled_task"
    boot_trigger = $false
    supervisor_pid = if ($null -eq $ConfirmedStatus) { $null } else { [int]$ConfirmedStatus.supervisor_pid }
}
$LauncherStateTemporary = "$LauncherStatePath.tmp.$PID"
$LauncherState | ConvertTo-Json -Compress | Set-Content -LiteralPath $LauncherStateTemporary -Encoding utf8
Move-Item -LiteralPath $LauncherStateTemporary -Destination $LauncherStatePath -Force

if ($null -eq $ConfirmedStatus) {
    $TaskState = (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).State
    throw "Persistent task launch was not confirmed within 30 seconds (task state: $TaskState). Do not retry until task and runtime status are inspected."
}
Set-Content -LiteralPath (Join-Path $RuntimePath "process.id") -Value $ConfirmedStatus.supervisor_pid -Encoding ascii
Write-Output "Started persistent Live supervisor $($ConfirmedStatus.supervisor_pid) through on-demand task $TaskName; inspect status files and logs."
