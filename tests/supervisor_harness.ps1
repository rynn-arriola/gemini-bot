param([string]$RuntimePath, [string]$SupervisorScript, [int]$Version, [string]$Results)
# Offline only: intercept every child-process launch and sleep. No bot runs.
$ErrorActionPreference = 'Stop'
$global:GeminiTestQueue = [System.Collections.Generic.Queue[string]]::new()
foreach ($Result in $Results.Split(',')) { $global:GeminiTestQueue.Enqueue($Result) }
$global:GeminiTestLaunches = 0
$global:GeminiTestSleeps = 0
$global:GeminiTestRuntime = $RuntimePath
$global:GeminiTestCooldown = $false
@{ permit_version=$Version; supervised_restart_limit=2 } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $RuntimePath 'permit.json')

function Start-Process {
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle, $RedirectStandardOutput, $RedirectStandardError, [switch]$PassThru)
    if ($ArgumentList -match '"run-live"') {
        if ($global:GeminiTestQueue.Count -eq 0) { throw 'Unexpected extra live child launch' }
        $global:GeminiTestLaunches++
        $Code = $global:GeminiTestQueue.Dequeue()
        $RetryBefore = $null
        if ($Code -eq 'cooldown') {
            $Code = '6'
            $global:GeminiTestCooldown = $true
            $RetryBefore = [DateTimeOffset]::UtcNow.AddHours(1).ToString('o')
        }
        $Match = [regex]::Match($ArgumentList, '"--supervisor-session-id" "([^"]+)"')
        $Session = $Match.Groups[1].Value
        if ($Code -ne 'missing') {
            $Outcome = switch ($Code) { '0' {'closed'} '5' {'recoverable_flat'} '6' {'startup_wait'} default {'failed'} }
            if ($Code -eq 'cross') { $Session = 'wrong-session'; $Code = '5'; $Outcome = 'recoverable_flat' }
            @{session_id=$Session; exit_code=[int]$Code; outcome=$Outcome; retry_not_before=$RetryBefore} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $global:GeminiTestRuntime 'runner-result.json')
        }
    }
    @{ launches=$global:GeminiTestLaunches; sleeps=$global:GeminiTestSleeps } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $global:GeminiTestRuntime 'harness-counts.json')
    $Fake = [pscustomobject]@{ Id=999999; ExitCode=0 }
    $Fake | Add-Member ScriptMethod WaitForExit { param($Timeout) return $true }
    $Fake | Add-Member ScriptMethod Refresh { }
    return $Fake
}
function Start-Sleep {
    param($Seconds)
    $global:GeminiTestSleeps++
    if ($global:GeminiTestSleeps -gt 500) { throw 'Supervisor loop did not converge in offline harness' }
    $Status = Get-Content -LiteralPath (Join-Path $global:GeminiTestRuntime 'supervisor-status.json') -Raw | ConvertFrom-Json
    if ($Status.state -eq 'ATTENTION_REQUIRED' -or ($global:GeminiTestCooldown -and $global:GeminiTestSleeps -ge 25)) {
        Set-Content -LiteralPath (Join-Path $global:GeminiTestRuntime 'SUPERVISOR_STOP') -Value 'offline test complete'
    }
}
& $SupervisorScript -EnvironmentPath 'fake-unused-env' -RuntimePath $RuntimePath -PermitPath (Join-Path $RuntimePath 'permit.json')
