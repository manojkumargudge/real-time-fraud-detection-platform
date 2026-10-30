<#
.SYNOPSIS
Register, enable, disable or remove the scheduled drift + retraining task (Windows Task Scheduler).

.DESCRIPTION
Default: registers task "FraudDrift-Retrain" that runs scripts\scheduled_drift.ps1 every
6 hours with its defaults (the repo's mlflow.db, the demo database from .env), as the current
user, only while that user is logged on (no stored password). It is registered DISABLED;
enable it with -Enable.

-Once -At <time> registers a single run instead (used for the proof run on copies), and
-ScriptArguments passes extra arguments to scheduled_drift.ps1.

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_drift_task.ps1            # register (disabled)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_drift_task.ps1 -Enable    # start the 6-hour schedule
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_drift_task.ps1 -Disable
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_drift_task.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$TaskName = "FraudDrift-Retrain",
    [int]$EveryHours = 6,
    [switch]$Once,
    [datetime]$At = (Get-Date).AddMinutes(5),
    [string]$ScriptArguments = "",
    [switch]$Enable,
    [switch]$Disable,
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($Unregister) {
    if ($existing) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false; "unregistered $TaskName" }
    else { "$TaskName is not registered" }
    exit 0
}
$definition = @("Once", "At", "ScriptArguments", "EveryHours") | Where-Object { $PSBoundParameters.ContainsKey($_) }
if (($Enable -or $Disable) -and $existing -and -not $definition) {
    # Only switch an existing task on or off; passing any schedule parameter re-registers it.
    if ($Enable) { Enable-ScheduledTask -TaskName $TaskName | Out-Null; "enabled $TaskName" }
    else { Disable-ScheduledTask -TaskName $TaskName | Out-Null; "disabled $TaskName" }
    Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo |
        Select-Object TaskName, LastRunTime, LastTaskResult, NextRunTime | Format-List
    exit 0
}

$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot "scheduled_drift.ps1"
$argument = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" $ScriptArguments".Trim()
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument -WorkingDirectory $repo
if ($Once) {
    $trigger = New-ScheduledTaskTrigger -Once -At $At
} else {
    $trigger = New-ScheduledTaskTrigger -Once -At $At -RepetitionInterval (New-TimeSpan -Hours $EveryHours)
}
# One run at a time; a missed run (laptop off) starts when the laptop is back; 2 h limit per run.
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$description = "Drift check + automatic retraining (src.monitoring.run_drift, rules in configs/drift.toml). " +
    "Registered by scripts/register_drift_task.ps1."
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description $description -Force | Out-Null

if ($Enable) { Enable-ScheduledTask -TaskName $TaskName | Out-Null; $state = "ENABLED" }
else { Disable-ScheduledTask -TaskName $TaskName | Out-Null; $state = "DISABLED" }
$when = if ($Once) { "once at $At" } else { "every $EveryHours h from $At" }
"registered $TaskName ($state, $when)"
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State | Format-List
