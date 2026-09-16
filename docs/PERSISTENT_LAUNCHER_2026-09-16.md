# Persistent Windows launcher repair — September 16, 2026

## Incident evidence

The user-started V11 run launched at approximately 09:14:30 Eastern. The last runtime heartbeat was 09:54:07 and the final normal order transitions were at 09:54:11. Both the Python runner and PowerShell supervisor were absent at the next check. There was no STOP/SUPERVISOR_STOP request, `runner-result.json`, stderr, structured error/critical event, Windows application error, or power event in the stop window. Gemini then showed zero active orders and the existing single position.

The launching PowerShell was the supervisor's parent, and the whole tree lasted about 40 minutes. This supports external termination of a temporary caller-owned Windows job as the strongest explanation. It does not prove the exact host mechanism, so the event remains classified as an unknown process-tree disappearance rather than a bot exception.

## Repair

`scripts/start_production.ps1 -Mode Live` no longer uses `Start-Process`. It creates a stable task name from the project path, refuses a running task or process-backed supervisor, and registers an on-demand Windows Scheduled Task with:

- no boot, logon, calendar, or retry trigger;
- unlimited Task Scheduler execution time;
- `IgnoreNew` multiple-instance behavior;
- the current interactive user at limited privilege;
- the system Windows PowerShell executable and exact supervisor arguments.

Task Scheduler owns the supervisor process, so closing or timing out the calling terminal does not intentionally terminate the bot tree. The launcher waits up to 30 seconds for a new process-backed supervisor heartbeat and refuses an ambiguous retry if confirmation is absent. It records only the task name, launch type, registration time, execution-time setting and supervisor PID. The environment contents, credentials and permit contents are never copied into launcher telemetry.

The task itself creates no permit, retries no mutation and cannot bypass single-owner locking, account reconciliation, STOP, HARD, attention, build/account/policy binding, startup standby or supervisor recovery rules. `Prepare` remains a non-ordering child process. `check_runtime.ps1` now reports task registration state alongside process and heartbeat evidence.

## Verification and limits

An offline harness replaces all Task Scheduler mutation commands, fails if Live calls `Start-Process`, and verifies the task action, unlimited duration, battery continuity, `IgnoreNew`, interactive limited principal, heartbeat confirmation and task metadata. A real non-trading scheduler probe slept for two seconds, was observed running, completed with Task Scheduler result zero and was then removed. Full verification passed 551 production and 28 script tests, Python compilation, every PowerShell parse, whitespace checks and offline Windows policy/durable-storage validation.

This repair prevents the known caller-job lifetime from owning Live. It does not auto-start at boot or logon, survive host shutdown/user logoff, recover an OS-killed process, create new live authority, or credit missed Gemini reward snapshots. Its 50% impact is only the reduction of avoidable launcher-caused gaps; all normal reward feasibility and safety rules remain unchanged.
