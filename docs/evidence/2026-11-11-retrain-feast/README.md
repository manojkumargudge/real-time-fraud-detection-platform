# Automatic retraining on the Feast offline store (2026-11-11)

**Change:** the drift job (`src/monitoring/run_drift.py`) now builds the challenger's training
matrix from the Feast offline store (`retraining_frame` -> `src.models.train.model_frame(...,
"feast")`), the same source the training CLI uses by default. `--retrain-feature-source pandas`
keeps the old path. The job compares the Feast rows with the batch rows it computed the checks
on and **fails** if they differ in any row, column, dtype, index or value.

Run on copies only: database `fraud_p2` on the test-stack PostgreSQL (restored from
`backups/fraud-demo-2026-11-11-preaudit.dump`: 118,108 / 5,093 / 9 / 20 / 6) and two copies
of `mlflow.db` (sha256 `dd198df9…`, equal to the real file) whose experiments'
`artifact_location` pointed at a folder next to each copy. The real `mlflow.db` and the demo
database were not written (real `mlflow.db` sha256 unchanged after the runs).

Command (both runs, same window, no `--force-retrain`):

```
python -m src.monitoring.run_drift --window-start-day 170 --window-end-day 182 \
    --retrain-feature-source pandas|feast --reports-dir <copy dir>
```

## Results

| | pandas path | Feast path |
|---|---|---|
| Trigger | fired: top-feature drift, R_emaildomain (`forced = false`) | same |
| Training matrix (rows <= label cutoff) | 505,109 rows x 31 columns | 505,109 rows x 31 columns |
| Matrix sha256 (`frame_sha256`: names, dtypes, index, values) | `5326f6fda279b2b6…54571` | `5326f6fda279b2b6…54571` (job asserted equality) |
| Challenger (copy registry) | v8, run `a01b584d…`, tag `feature_source = pandas` | v8, run `e7d2fbc7…`, tag `feature_source = feast` |
| Challenger `model.json` sha256 | `5dfeb952e74c3939…a458f7f` | `5dfeb952e74c3939…a458f7f` (identical) |
| Holdout PR-AUC challenger / champion | 0.3290 / 0.3238 | 0.3290 / 0.3238 |
| Promotion | NO (CI95 [-0.0160, +0.0254], lower bound <= 0.01) | same |
| Aliases after (copy) | champion 2, production 2, challenger 8 | same |
| Runtime (whole job) | 294 s | **2,726 s (45.4 min)**, of which the Feast matrix 2,488 s |

The Feast run was the only heavy job on the laptop (i5-1235U, 16 GB, AC power).

## Cost

Building the matrix from Feast adds about 41 minutes to a run that retrains; a run that does not
fire never builds it (the checks still use the batch frame). Every 6 h (Task Scheduler) this is
affordable. `--retrain-feature-source pandas` gives the same model in about 5 minutes.

## Files

- `run_pandas.log`, `run_feast.log`: job logs (the 127 per-chunk "feast chunk rows=" lines and
  Feast's "Registry cache expired" lines are removed from the Feast log).
- `challenger_model_sha256.txt`: sha256 of both challengers' `model.json`.
- Tests: `tests/test_monitoring.py` (Feast frame equals the batch frame, mismatch fails the job,
  unknown source rejected, the job hands the Feast rows to `retrain_and_compare` with tag
  `feature_source = feast`, default is Feast); `tests/test_integration.py::
  test_drift_job_end_to_end_writes_drift_reports` now retrains on the Feast path with a real
  MLflow store and asserts the run tag.
