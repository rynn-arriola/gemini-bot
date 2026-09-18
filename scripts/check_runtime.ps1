param([Parameter(Mandatory = $true)][string]$RuntimeDirectory, [switch]$Logs)

# Read-only, no API calls and no process control. A saved RUNNING label is not
# liveness evidence. Run from any directory using a project-relative data path.
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimePath = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $RuntimeDirectory))
$DataRoot = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot "data"))
if (-not $RuntimePath.StartsWith($DataRoot + [System.IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "RuntimeDirectory must resolve below this project's data directory."
}

function Read-JsonFile([string]$Name) {
    $Path = Join-Path $RuntimePath $Name
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try { return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json } catch { return $null }
}
function Is-Fresh($Document, [int]$Seconds) {
    if ($null -eq $Document -or -not $Document.updated_at) { return $false }
    try {
        $Age = ([DateTimeOffset]::UtcNow - [DateTimeOffset]::Parse($Document.updated_at)).TotalSeconds
        return $Age -ge 0 -and $Age -le $Seconds
    } catch { return $false }
}
function Process-Exists($Value) {
    if ($null -eq $Value) { return $false }
    try { return $null -ne (Get-Process -Id ([int]$Value) -ErrorAction SilentlyContinue) } catch { return $false }
}

$Supervisor = Read-JsonFile "supervisor-status.json"
$RunnerStatus = Read-JsonFile "status.json"
$RunnerResultDocument = Read-JsonFile "runner-result.json"
$LauncherTask = Read-JsonFile "launcher-task.json"
$SupervisorFresh = Is-Fresh $Supervisor 45
$RunnerFresh = Is-Fresh $RunnerStatus 90
$RunnerExists = Process-Exists $Supervisor.runner_pid
$SupervisorExists = Process-Exists $Supervisor.supervisor_pid
$PersistentTaskState = $null
if ($null -ne $LauncherTask -and $LauncherTask.task_name) {
    try {
        $PersistentTaskState = [string](Get-ScheduledTask -TaskName ([string]$LauncherTask.task_name) -ErrorAction Stop).State
    } catch { $PersistentTaskState = $null }
}
$Healthy = $SupervisorFresh -and $RunnerFresh -and $RunnerExists -and $SupervisorExists -and $Supervisor.state -eq "RUNNING" -and $RunnerStatus.phase -in @("quoting", "recovering", "standby")
[ordered]@{
    observation = if ($Healthy -and ($RunnerStatus.phase -eq "recovering" -or @($RunnerStatus.entry_read_holds).Count -gt 0)) { "FRESH_RECOVERY_HOLD" } elseif ($Healthy -and $RunnerStatus.phase -eq "standby") { "FRESH_STANDBY_HEARTBEATS" } elseif ($Healthy) { "FRESH_QUOTING_HEARTBEATS" } elseif ($SupervisorFresh -and $SupervisorExists -and $Supervisor.state -eq "BACKOFF") { "FRESH_SUPERVISED_BACKOFF" } else { "ATTENTION_REQUIRED_OR_STOPPED" }
    entry_read_holds = $RunnerStatus.entry_read_holds
    stream_recovery_holds = $RunnerStatus.stream_recovery_holds
    journal_backup = $RunnerStatus.journal_backup
    supervisor_fresh = $SupervisorFresh
    runner_fresh = $RunnerFresh
    supervisor_process_exists = $SupervisorExists
    runner_process_exists = $RunnerExists
    saved_supervisor_state = $Supervisor.state
    saved_runner_phase = $RunnerStatus.phase
    last_runner_update = $RunnerStatus.updated_at
    last_runner_reason = $RunnerStatus.reason
    saved_result_outcome = $RunnerResultDocument.outcome
    launcher_kind = $LauncherTask.launch_kind
    persistent_task_name = $LauncherTask.task_name
    persistent_task_state = $PersistentTaskState
    venue_state_verified = $false
} | ConvertTo-Json
if ($Logs) {
    foreach ($LogPath in @((Join-Path $RuntimePath "supervisor.jsonl"), (Join-Path $RuntimePath "logs\gemini-bot.jsonl"))) {
        if (Test-Path -LiteralPath $LogPath -PathType Leaf) { Get-Content -LiteralPath $LogPath -Tail 20 }
    }
}
