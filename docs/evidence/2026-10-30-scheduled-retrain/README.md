# Scheduled retraining: proof run started by Task Scheduler (2026-10-30)

The drift job with automatic retraining, started by **Windows Task Scheduler** (not by hand),
through the same scripts the real schedule uses (`scripts/register_drift_task.ps1` ->
`scripts/scheduled_drift.ps1` -> `python -m src.monitoring.run_drift`, retraining on, **no
`--force-retrain`**), on **copies only**. Drift rules unchanged (`configs/drift.toml` sha256
`7a7f1e64…`, the same as on 2026-10-24 and 2026-10-27); no `src/` change.

## Setup (copies, the same method as docs/evidence/2026-10-27-drift-trigger/)

- **Database:** `fraud_evidence` on the test-stack PostgreSQL (localhost:25432), restored from
  `backups/fraud-demo-2026-10-30-pre-auto-retrain.dump` (local only; 118,108 / 5,093 / 9 / 20 /
  6, the demo database's counts).
- **MLflow:** a copy of `mlflow.db` (sha256 `dd198df9…`, equal to the real file). In the copy
  only, every experiment's `artifact_location` was pointed at a folder next to the copy, so new
  runs could not land in the real `mlruns/`. (On 2026-10-27 the copy still pointed at the real
  `mlruns/`, so that run's artifacts landed there; this time they did not.)
- **Task:** one-time task `FraudDrift-Evidence` registered with
  `register_drift_task.ps1 -TaskName FraudDrift-Evidence -Once -At <now + 2 min> -Enable
  -ScriptArguments "-WindowStartDay 170 -WindowEndDay 182 -MlflowTrackingUri <copy>
  -DatabaseUrl <fraud_evidence> -ReportsDir <copy dir> -LogDir <copy dir>"`, unregistered after.
- **Window:** days 170-181 (`--window-end-day` is exclusive), the window that fired on
  R_emaildomain on 2026-10-27 (chosen then *after* the default window did not fire; this run
  reuses it to prove the schedule, not to re-test the trigger).

## Runs (all started by Task Scheduler)

| Scheduled at | Commit | LastTaskResult | What happened | Log |
|---|---|---|---|---|
| 11:27:59 | 31c8a4e | 1 | **Script bug:** exited in 0 s. Windows PowerShell re-escaped the quotes passed to `cmd.exe /c`, so the interpreter was never started; nothing was written. Fixed in 595474f (Python is called directly). | `scheduled_run_1_failed_cmd_quoting.log` |
| 11:31:30 | 1c7aa87 | **0** | Fired on R_emaildomain (drift_reports **id 10**, `forced = false`); challenger trained and registered as **v8** (alias `challenger`, copy only); promotion **NO**. 400 s. | `scheduled_run_2_retrained.log` (mlflow progress bars appear as garbled characters; fixed in 2b152bb) |
| 11:41:08 | 2b152bb | **0** | Same window, final script: fired again (id **11**), **reused challenger v8** (same training rows, no retrain), promotion **NO**. 237 s. | `scheduled_run_3_reused.log`, `task_info.txt` |

A second scheduling bug was found on the way: `-Enable` on an existing task only re-enabled it
and ignored a new `-At`; fixed in 1c7aa87 (schedule parameters always re-register).

## Result

- **Trigger:** "top-feature drift: R_emaildomain drifted (among the champion's top 5 features
  by gain)"; share 0.286, prediction PSI 0.0025, delayed PR-AUC 0.3238
  (`drift_reports_rows_10_11.json`).
- **Challenger v8** (copy only): MLflow run `a827a7b6d79f424ba67905e1a5eec473`, tags
  `git_commit 1c7aa87…`, `git_dirty false`, `forced false`; artifacts in the copy's folder
  (`challenger_run.json`).
- **Promotion gate:** challenger 0.3290 vs champion 0.3238, delta +0.0053, 95% CI
  [-0.0160, +0.0254]; lower bound below the 0.01 margin -> **champion kept**. Identical to the
  manual run on 2026-10-27. `production` is never moved.
- **Copy registry after:** champion 2, production 2, challenger 8.

## Real data untouched (checked after the runs)

- Real `mlflow.db` sha256 `dd198df9…` (unchanged); aliases champion 2, production 2,
  challenger 7; 7 model_versions rows; run `a827a7b6` not in it.
- Real `mlruns/`: 42 entries before and after, nothing new.
- Demo database: 118,108 / 5,093 / 9 / 20 / 6.
- `artifacts/baseline/model.json` `9fdad27e…`, `metadata.json` `a65a6dfc…`,
  `artifacts/gnn/scores.parquet` `041078eb…`: unchanged.

## Limits

- Runs only while the laptop is on, awake and the user is logged on (task runs as the user,
  no stored password); a missed run starts when the laptop is back (`StartWhenAvailable`).
- Host only: the k8s CronJob still runs checks only (`--no-retrain`); in-cluster retraining
  needs an MLflow server with a shared artifact store.
- The proof used a window known to fire; the trigger itself is still uncalibrated (README
  "Known limitations").
