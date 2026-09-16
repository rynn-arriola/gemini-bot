param(
    [Parameter(Mandatory = $true)]
    [string]$RuntimeDirectory,
    [Parameter(Mandatory = $true)]
    [string]$EnvironmentFile,
    [Parameter(Mandatory = $true)]
    [string]$PermitFile
)

# Offline harness: replace every Task Scheduler mutation and fail if Live falls
# back to Start-Process. The production script still exercises its validation,
# quoting, confirmation, and metadata paths.
$ErrorActionPreference = "Stop"
$global:LauncherHarnessRuntime = [System.IO.Path]::GetFullPath($RuntimeDirectory)
$global:LauncherHarnessRegistered = $false
$global:LauncherHarnessStarted = $false
$global:LauncherHarnessAction = $null
$global:LauncherHarnessSettings = $null
$global:LauncherHarnessPrincipal = $null
$global:LauncherHarnessTaskName = $null

function Start-Process { throw "Live launcher must not use Start-Process" }
function Get-ScheduledTask {
    param([string]$TaskName, $ErrorAction)
    if ($global:LauncherHarnessStarted) { return [pscustomobject]@{ State = "Running" } }
    return $null
}
function New-ScheduledTaskAction {
    param([string]$Execute, [string]$Argument, [string]$WorkingDirectory)
    $global:LauncherHarnessAction = [pscustomobject]@{
        Execute = $Execute
        Argument = $Argument
        WorkingDirectory = $WorkingDirectory
    }
    return $global:LauncherHarnessAction
}
function New-ScheduledTaskSettingsSet {
    param(
        [switch]$AllowStartIfOnBatteries,
        [switch]$DontStopIfGoingOnBatteries,
        [switch]$StartWhenAvailable,
        [TimeSpan]$ExecutionTimeLimit,
        [string]$MultipleInstances
    )
    $global:LauncherHarnessSettings = [pscustomobject]@{
        AllowStartIfOnBatteries = [bool]$AllowStartIfOnBatteries
        DontStopIfGoingOnBatteries = [bool]$DontStopIfGoingOnBatteries
        StartWhenAvailable = [bool]$StartWhenAvailable
        ExecutionTimeLimit = $ExecutionTimeLimit.ToString()
        MultipleInstances = $MultipleInstances
    }
    return $global:LauncherHarnessSettings
}
function New-ScheduledTaskPrincipal {
    param([string]$UserId, [string]$LogonType, [string]$RunLevel)
    $global:LauncherHarnessPrincipal = [pscustomobject]@{
        UserId = $UserId
        LogonType = $LogonType
        RunLevel = $RunLevel
    }
    return $global:LauncherHarnessPrincipal
}
function New-ScheduledTask {
    param($Action, $Settings, $Principal)
    return [pscustomobject]@{ Action = $Action; Settings = $Settings; Principal = $Principal }
}
function Register-ScheduledTask {
    param([string]$TaskName, $InputObject, [switch]$Force)
    $global:LauncherHarnessRegistered = $true
    $global:LauncherHarnessTaskName = $TaskName
    return [pscustomobject]@{ TaskName = $TaskName }
}
function Start-ScheduledTask {
    param([string]$TaskName)
    $global:LauncherHarnessStarted = $true
    [ordered]@{
        updated_at = [DateTimeOffset]::UtcNow.ToString("o")
        state = "RUNNING"
        supervisor_pid = $PID
    } | ConvertTo-Json -Compress | Set-Content -LiteralPath `
        (Join-Path $global:LauncherHarnessRuntime "supervisor-status.json") -Encoding utf8
}

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Output = & (Join-Path $ProjectRoot "scripts\start_production.ps1") `
    -Mode Live -EnvironmentFile $EnvironmentFile -RuntimeDirectory $RuntimeDirectory `
    -ActivationPermit $PermitFile
$LauncherState = Get-Content -LiteralPath `
    (Join-Path $global:LauncherHarnessRuntime "launcher-task.json") -Raw | ConvertFrom-Json

[ordered]@{
    output = [string]$Output
    registered = $global:LauncherHarnessRegistered
    started = $global:LauncherHarnessStarted
    task_name = $global:LauncherHarnessTaskName
    action = $global:LauncherHarnessAction
    settings = $global:LauncherHarnessSettings
    principal = $global:LauncherHarnessPrincipal
    launcher_state = $LauncherState
    process_id = [int](Get-Content -LiteralPath (Join-Path $global:LauncherHarnessRuntime "process.id") -Raw)
    harness_pid = $PID
} | ConvertTo-Json -Depth 5 -Compress
