<#
.SYNOPSIS
Scheduled drift check with automatic retraining (host only, run by Windows Task Scheduler).

.DESCRIPTION
Runs `python -m src.monitoring.run_drift` with retraining enabled and without --force-retrain:
the drift rules in configs/drift.toml decide whether a challenger is trained, and the bootstrap
promotion gate decides whether `champion` moves (`production` never moves). Registered by
scripts/register_drift_task.ps1 (task "FraudDrift-Retrain", every 6 hours).

- Uses the project's Python (.venv), sets PYTHONUTF8=1 (and PYTHONUNBUFFERED=1 for an ordered
  log) and runs from the repo root.
- Database: -DatabaseUrl, else $env:DATABASE_URL, else the demo database built from .env
  (-DatabaseName, default "fraud"). The password is never written to the log.
- MLflow: -MlflowTrackingUri, else $env:MLFLOW_TRACKING_URI, else the repo's mlflow.db
  (src.models.tracking).
- Checks that PostgreSQL is reachable first (exit code 2 if not).
- Logs everything to logs/scheduled-drift-<timestamp>.log and exits with the job's exit code.

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\scheduled_drift.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\scheduled_drift.ps1 -WindowStartDay 170 -WindowEndDay 182
#>
[CmdletBinding()]
param(
    [Nullable[double]]$WindowStartDay,
    [Nullable[double]]$WindowEndDay,  # exclusive, as in run_drift
    [string]$MlflowTrackingUri,
    [string]$DatabaseUrl,
    [string]$DatabaseName = "fraud",
    [string]$ReportsDir,
    [string]$LogDir
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Set-Location $Repo
$env:PYTHONUTF8 = "1"
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not $LogDir) { $LogDir = Join-Path $Repo "logs" }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir ("scheduled-drift-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))

function Write-Log([string]$Message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-ddTHH:mm:ss"), $Message
    Add-Content -Path $Log -Value $line -Encoding UTF8
    Write-Output $line
}

function Hide-Password([string]$Url) {
    return ($Url -replace '(postgres(?:ql)?://[^:/@]+:)[^@]*@', '$1***@')
}

function Get-DemoDatabaseUrl([string]$Name) {
    $envFile = Join-Path $Repo ".env"
    if (-not (Test-Path $envFile)) { throw ".env not found: cannot build the demo DATABASE_URL" }
    $values = @{}
    foreach ($line in Get-Content $envFile -Encoding UTF8) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$' -and -not $line.TrimStart().StartsWith("#")) {
            $values[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
        }
    }
    if (-not $values.ContainsKey("POSTGRES_PASSWORD")) { throw "POSTGRES_PASSWORD missing in .env" }
    $port = if ($values["POSTGRES_HOST_PORT"]) { $values["POSTGRES_HOST_PORT"] } else { "15432" }
    $password = [uri]::EscapeDataString($values["POSTGRES_PASSWORD"])
    return "postgresql://fraud:$password@localhost:$port/$Name"
}

try {
    Write-Log "scheduled drift job start (host $env:COMPUTERNAME, user $env:USERNAME)"
    Write-Log ("repo HEAD {0}" -f (git -C $Repo rev-parse --short HEAD 2>$null))
    if (-not (Test-Path $Python)) { throw "project Python not found: $Python" }

    if ($DatabaseUrl) { $env:DATABASE_URL = $DatabaseUrl; $source = "-DatabaseUrl" }
    elseif ($env:DATABASE_URL) { $source = "environment" }
    else { $env:DATABASE_URL = Get-DemoDatabaseUrl $DatabaseName; $source = ".env ($DatabaseName)" }
    Write-Log ("DATABASE_URL from {0}: {1}" -f $source, (Hide-Password $env:DATABASE_URL))

    if ($MlflowTrackingUri) { $env:MLFLOW_TRACKING_URI = $MlflowTrackingUri }
    $mlflowShown = if ($env:MLFLOW_TRACKING_URI) { $env:MLFLOW_TRACKING_URI } else { "default (repo mlflow.db)" }
    Write-Log "MLFLOW_TRACKING_URI: $mlflowShown"
} catch {
    Write-Log "ERROR: $($_.Exception.Message)"
    exit 3
}

# PostgreSQL must be reachable before the (long) job starts.
$check = "import os, psycopg; c = psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=5); " +
    "print(c.execute('select current_database()').fetchone()[0])"
$ErrorActionPreference = "Continue"
$db = & $Python -c $check 2>&1
$checkExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($checkExit -ne 0) {
    Write-Log "ERROR: PostgreSQL is not reachable at $(Hide-Password $env:DATABASE_URL). Is the stack running (docker compose start postgres)?"
    Write-Log ("psycopg: {0}" -f (($db | Select-Object -Last 1) -replace 'postgres(?:ql)?://\S+', '<url>'))
    exit 2
}
Write-Log "PostgreSQL reachable: database $db"

$arguments = @("-m", "src.monitoring.run_drift")
if ($null -ne $WindowStartDay) { $arguments += @("--window-start-day", "$WindowStartDay") }
if ($null -ne $WindowEndDay) { $arguments += @("--window-end-day", "$WindowEndDay") }
if ($ReportsDir) { $arguments += @("--reports-dir", $ReportsDir) }
Write-Log ("run: python {0}" -f ($arguments -join " "))

# Both streams go to the log as plain lines, in order (unbuffered); $LASTEXITCODE is the job's.
# (Not via cmd.exe /c: Windows PowerShell re-escapes embedded quotes, which breaks the command.)
$env:PYTHONUNBUFFERED = "1"
$env:MLFLOW_ENABLE_ARTIFACTS_PROGRESS_BAR = "false"  # tqdm bars are unreadable in a log file
$started = Get-Date
$ErrorActionPreference = "Continue"
& $Python @arguments 2>&1 |
    ForEach-Object { if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { "$_" } } |
    Add-Content -Path $Log -Encoding UTF8
$exitCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"
Write-Log ("run_drift exit code {0} after {1:N0} s" -f $exitCode, ((Get-Date) - $started).TotalSeconds)
exit $exitCode
