param(
    [Parameter(Mandatory = $true)]
    [string]$EnvironmentPath,
    [Parameter(Mandatory = $true)]
    [string]$RuntimePath,
    [Parameter(Mandatory = $true)]
    [string]$PermitPath,
    [switch]$IgnoreUptimeProxy
)

# Windows-native supervision. V5/6 retain bounded-session behavior. V7 waits
# through explicit read-only startup outages and allows paced transient-flat
# recovery in this same session; integrity/unknown/HARD stays in attention.
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$SupervisorStatus = Join-Path $RuntimePath "supervisor-status.json"
$SupervisorStop = Join-Path $RuntimePath "SUPERVISOR_STOP"
$RunnerResult = Join-Path $RuntimePath "runner-result.json"
$SupervisorLog = Join-Path $RuntimePath "supervisor.jsonl"
$LastLoggedState = ""
$LastLoggedAt = [DateTimeOffset]::MinValue

function Write-SupervisorLog($Payload) {
    # Bounded, structured lifecycle history; never log environment/permit data.
    if ((Test-Path -LiteralPath $SupervisorLog) -and (Get-Item -LiteralPath $SupervisorLog).Length -ge 2097152) {
        for ($Index = 2; $Index -ge 1; $Index--) {
            $Source = "$SupervisorLog.$Index"
            if (Test-Path -LiteralPath $Source) {
                Move-Item -LiteralPath $Source -Destination "$SupervisorLog.$($Index + 1)" -Force
            }
        }
        Move-Item -LiteralPath $SupervisorLog -Destination "$SupervisorLog.1" -Force
    }
    $Payload | ConvertTo-Json -Compress | Add-Content -LiteralPath $SupervisorLog -Encoding utf8
}

function Write-SupervisorStatus([string]$State, [string]$Reason, [int]$RunnerExitCode) {
    $Payload = [ordered]@{
        updated_at = [DateTimeOffset]::UtcNow.ToString("o")
        state = $State
        reason = $Reason
        runner_exit_code = $RunnerExitCode
        restart_count = $RestartCount
        consecutive_live_restarts = $RestartBurst
        continuous_service = $ContinuousService
        automatic_live_restart = $State -in @("STARTING", "RUNNING", "BACKOFF")
        supervisor_pid = $PID
        runner_pid = if ($null -eq $Runner) { $null } else { $Runner.Id }
        session_id = $SessionId
        heartbeat_ttl_seconds = 45
    }
    $Temporary = "$SupervisorStatus.tmp.$PID"
    $Payload | ConvertTo-Json -Compress | Set-Content -LiteralPath $Temporary -Encoding utf8
    Move-Item -LiteralPath $Temporary -Destination $SupervisorStatus -Force
    if ($State -ne $script:LastLoggedState -or ([DateTimeOffset]::UtcNow - $script:LastLoggedAt).TotalSeconds -ge 60) {
        Write-SupervisorLog $Payload
        $script:LastLoggedState = $State
        $script:LastLoggedAt = [DateTimeOffset]::UtcNow
    }
}

trap {
    # Do not guess flatness or restart after an unexpected supervisor failure.
    try {
        Write-SupervisorStatus "ATTENTION_REQUIRED" ("supervisor exception: " + $_.Exception.GetType().Name + "; runner/account state must be checked") 4
    } catch { Write-Error "Supervisor diagnostics failed; inspect runner and venue independently." -ErrorAction Continue }
    exit 4
}

function Join-QuotedArguments([string[]]$Values) {
    return (($Values | ForEach-Object {
        if ($_ -match '"') { throw "Argument contains an unsupported quote." }
        '"' + $_.TrimEnd('\') + '"'
    }) -join ' ')
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Project virtual-environment Python was not found."
}
$PermitDocument = Get-Content -LiteralPath $PermitPath -Raw | ConvertFrom-Json
if ($PermitDocument.permit_version -notin @(5, 6, 7)) {
    throw "Windows supervision requires a version-5/6 bounded or version-7 continuous permit."
}
$ContinuousService = $PermitDocument.permit_version -eq 7
$MaximumRestarts = [int]$PermitDocument.supervised_restart_limit
if ($MaximumRestarts -lt 1 -or $MaximumRestarts -gt 2) {
    throw "Bounded-session restart limit must be one or two."
}
$SessionId = [Guid]::NewGuid().ToString()
$RestartCount = 0
$RestartBurst = 0
$FastCrashCount = 0
$BackoffSeconds = 10
$FastCrashSeconds = 120
Remove-Item -LiteralPath $SupervisorStop -Force -ErrorAction SilentlyContinue
Write-SupervisorStatus "STARTING" "launching approved live runner" -1

$LastRunnerExitCode = -1
while ($true) {
    $RetryNotBefore = [DateTimeOffset]::MinValue
    $AttemptNumber = $RestartCount + 1
    $LogSlot = if ($ContinuousService) { (($AttemptNumber - 1) % 3) + 1 } else { $AttemptNumber }
    $RunnerOut = Join-Path $RuntimePath ("runner-{0:D2}.stdout.log" -f $LogSlot)
    $RunnerErr = Join-Path $RuntimePath ("runner-{0:D2}.stderr.log" -f $LogSlot)
    Remove-Item -LiteralPath $RunnerResult -Force -ErrorAction SilentlyContinue
    $Arguments = @(
        "-m", "gemini_bot", "run-live", "--env", $EnvironmentPath,
        "--directory", $RuntimePath, "--activation-permit", $PermitPath,
        "--supervisor-session-id", $SessionId
    )
    if ($IgnoreUptimeProxy) { $Arguments += "--ignore-uptime-proxy" }
    $StartedAt = [DateTimeOffset]::UtcNow
    $Runner = Start-Process -FilePath $Python -ArgumentList (Join-QuotedArguments $Arguments) `
        -WorkingDirectory $ProjectRoot -WindowStyle Hidden `
        -RedirectStandardOutput $RunnerOut -RedirectStandardError $RunnerErr -PassThru
    Set-Content -LiteralPath (Join-Path $RuntimePath "runner.id") -Value $Runner.Id -Encoding ascii
    Write-SupervisorStatus "RUNNING" "approved runner is active" -1
    while (-not $Runner.WaitForExit(10000)) {
        Write-SupervisorStatus "RUNNING" "runner process exists; check runner heartbeat separately" -1
    }
    $Runner.Refresh()
    # Copy immediately into a dedicated scalar. Do not reuse a generic
    # ExitCode variable across the later recovery child process.
    $LastRunnerExitCode = [int]$Runner.ExitCode
    if (-not (Test-Path -LiteralPath $RunnerResult -PathType Leaf)) {
        # A missing durable classification is an abnormal process boundary,
        # never a clean or automatically recoverable exit.
        $LastRunnerExitCode = 4
    } else {
        try {
            $RunnerResultDocument = Get-Content -LiteralPath $RunnerResult -Raw | ConvertFrom-Json
            $RecordedSession = [string]$RunnerResultDocument.session_id
            $RecordedExitCode = [int]$RunnerResultDocument.exit_code
            $RecordedOutcome = [string]$RunnerResultDocument.outcome
            $ValidOutcome = (
                ($RecordedExitCode -eq 0 -and $RecordedOutcome -eq "closed") -or
                ($RecordedExitCode -eq 5 -and $RecordedOutcome -eq "recoverable_flat") -or
                ($ContinuousService -and $RecordedExitCode -eq 6 -and $RecordedOutcome -eq "startup_wait") -or
                ($RecordedExitCode -in @(2, 3) -and $RecordedOutcome -in @("failed", "startup_or_unclassified_failure"))
            )
            if ($RecordedSession -ne $SessionId -or -not $ValidOutcome) {
                $LastRunnerExitCode = 4
            } else {
                $LastRunnerExitCode = $RecordedExitCode
                if ($RunnerResultDocument.retry_not_before) {
                    $RetryNotBefore = [DateTimeOffset]::Parse([string]$RunnerResultDocument.retry_not_before)
                }
            }
        } catch {
            $LastRunnerExitCode = 4
        }
    }
    $RanSeconds = ([DateTimeOffset]::UtcNow - $StartedAt).TotalSeconds

    if (Test-Path -LiteralPath $SupervisorStop) {
        Write-SupervisorStatus "STOPPED" "operator stop completed" $LastRunnerExitCode
        exit 0
    }
    if ($LastRunnerExitCode -eq 0) {
        if ($ContinuousService) { break } # HARD/other drain stays online for attention, never auto-clears.
        Write-SupervisorStatus "STOPPED" "runner exited clean and flat" $LastRunnerExitCode
        exit 0
    }
    if ($LastRunnerExitCode -ne 5 -and -not ($ContinuousService -and $LastRunnerExitCode -eq 6)) { break }

    if ($LastRunnerExitCode -eq 6) {
        # Explicit read-only venue/catalog wait, not a crashing live runner.
    } elseif ($RanSeconds -lt $FastCrashSeconds) {
        $FastCrashCount += 1
    } else {
        $FastCrashCount = 0
        $RestartBurst = 0
        $BackoffSeconds = 10
    }
    if ($FastCrashCount -ge 3) { break }
    if (-not $ContinuousService -and $RestartCount -ge $MaximumRestarts) { break }
    if ($ContinuousService -and $LastRunnerExitCode -eq 5 -and $RestartBurst -ge $MaximumRestarts) { break }
    if ($LastRunnerExitCode -eq 5) { $RestartBurst += 1 }
    $RestartCount += 1
    $WaitReason = if ($LastRunnerExitCode -eq 6) { "read-only startup waiting for venue/catalog; no trading" } else { "verified transient flat exit; paced recovery pending" }
    Write-SupervisorStatus "BACKOFF" $WaitReason $LastRunnerExitCode
    for ($Waited = 0; $Waited -lt $BackoffSeconds -or [DateTimeOffset]::UtcNow -lt $RetryNotBefore; $Waited += 1) {
        if (Test-Path -LiteralPath $SupervisorStop) {
            Write-SupervisorStatus "STOPPED" "operator stop during recovery backoff" $LastRunnerExitCode
            exit 0
        }
        Start-Sleep -Seconds 1
        if (($Waited % 10) -eq 0) { Write-SupervisorStatus "BACKOFF" $WaitReason $LastRunnerExitCode }
    }
    $BackoffSeconds = [Math]::Min(60, $BackoffSeconds * 2)
}

# Non-recoverable, non-flat, unknown, exhausted, or repeated-fast failures do
# not relaunch. One read-only check records whether the venue is currently safe.
$RecoveryDirectory = Join-Path $RuntimePath "supervisor-recovery"
Write-SupervisorStatus "RECONCILING" "runner stopped without reusable flat proof; read-only check only" $LastRunnerExitCode
New-Item -ItemType Directory -Path $RecoveryDirectory -Force | Out-Null
$RecoveryArguments = @(
    "-m", "gemini_bot", "prepare-live", "--env", $EnvironmentPath,
    "--directory", $RecoveryDirectory
)
$Recovery = Start-Process -FilePath $Python -ArgumentList (Join-QuotedArguments $RecoveryArguments) `
    -WorkingDirectory $ProjectRoot -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $RecoveryDirectory "prepare.stdout.log") `
    -RedirectStandardError (Join-Path $RecoveryDirectory "prepare.stderr.log") -PassThru
while (-not $Recovery.WaitForExit(10000)) {
    Write-SupervisorStatus "RECONCILING" "read-only account check; trading remains blocked" $LastRunnerExitCode
}
$Recovery.Refresh()
$Reason = if ($Recovery.ExitCode -eq 0) {
    "automatic restart blocked or exhausted; read-only reconciliation passed; fresh approval required"
} else {
    "automatic restart blocked; read-only reconciliation failed; manual account review required"
}
Write-SupervisorStatus "ATTENTION_REQUIRED" $Reason $LastRunnerExitCode
while (-not (Test-Path -LiteralPath $SupervisorStop)) {
    Start-Sleep -Seconds 10
    Write-SupervisorStatus "ATTENTION_REQUIRED" $Reason $LastRunnerExitCode
}
Write-SupervisorStatus "STOPPED" "operator dismissed supervisor attention" $LastRunnerExitCode
