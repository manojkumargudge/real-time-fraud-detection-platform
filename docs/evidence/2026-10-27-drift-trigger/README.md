# Retraining trigger: clean evidence run (2026-10-27)

The drift job, run under the frozen rules (`configs/drift.toml`, sha256 `7a7f1e64…`, unchanged
since 2026-10-24) from a clean checkout of main **0d73adb**, **without `--force-retrain`**, on
copies only:

- **Database:** `fraud_evidence` on the test-stack PostgreSQL (localhost:25432), restored from
  `backups/fraud-demo-2026-10-27-eod.dump` (118,108 / 5,093 / 9 / 20 / 6, the same as the
  demo DB).
- **MLflow:** `MLFLOW_TRACKING_URI` pointed at a copy of `mlflow.db` (sha256 `3e8da794…`).
- **Untouched:** the demo database and the real `mlflow.db`; counts and hash were checked
  after the runs.

## Run 1: no flags (the automatic default window), which did not fire

`python -m src.monitoring.run_drift` → drift_reports **id 10** (in the copy):

- Current window: days 176–183, the last 7 days before now = day 182.
- 5 of 28 features drifted (share 0.179 ≤ 0.5). Prediction PSI 0.0027 (< 0.2).
- Delayed-label PR-AUC 0.3238 (≥ 0.2992 − 0.05).
- **No feature among the champion's top 5 by gain drifted** (`drifted_top_gain_features = []`).
  The drift is in DeviceInfo and the uid velocity features.
- **`triggered = false`**, so no retraining.

## Run 2: the fallback window, which fired

`python -m src.monitoring.run_drift --window-start-day 170 --window-end-day 182` (the window of
drift_reports id 6 from 2026-10-24) → **id 11**, `forced = false`, `synthetic = false`:

- **Trigger:** "top-feature drift: R_emaildomain drifted (among the champion's top 5 features
  by gain)". Share 0.286; PSI 0.0025; delayed PR-AUC 0.3238.
- **Challenger:**
  - Trained on days 1–146 and registered as **v8** (alias `challenger`).
  - MLflow run `f9bb4e91f6a64e4a84a153bb8b806321`, tags `git_commit 0d73adb…`,
    **`git_dirty false`**.
  - The reuse guard did not apply (v7 has no data tags), so a new challenger was trained.
- **Promotion (paired day-block bootstrap, 1,000 resamples, unit = day, 8 days):**
  - Holdout days 146–153: 20,264 rows, 617 frauds.
  - Challenger PR-AUC 0.3290 vs champion 0.3238: delta +0.0053, 95% CI [−0.0160, +0.0254].
  - The lower bound −0.0160 is below the 0.01 margin, so **the champion was kept**.
- **Aliases afterwards (copy):** champion = **v2**, production = **v2**, challenger = v8.
  Versions 1, 2, 7, 8.

## Caveats

- The automatic default window did **not** fire. The fire came from the explicit 170–182
  window.
- The firing rule (top-5 gain features) was added on 2026-10-24 after the first runs did not
  fire. It is noisy: it fired on every window tried that day.
- The challenger training is identical to 2026-10-24's (the same labels, seed and holdout), so
  the same metrics and the same decision are expected. They match.
- **Side effect:** the copied registry points at the real `mlruns/` folder, so the challenger's
  artifacts are in `mlruns/f9bb4e91f6a64e4a84a153bb8b806321/` (gitignored, additive). The real
  `mlflow.db` does not reference that run.

## Files

| File | What |
|---|---|
| `pre_run.txt` | git branch, HEAD, porcelain count, the drift.toml and mlflow.db hashes, and the aliases before |
| `run_drift_default.log`, `run_drift_fallback.log` | full job logs (tee'd; password masked) |
| `row10_default_window_*.html`, `row11_fallback_window_*.html` | Evidently reports |
| `drift_reports_rows_10_11.json` | both drift_reports rows (including `promotion` with the bootstrap) |
| `challenger_run.json` | challenger run id, tags, metrics, params, and the aliases and versions afterwards |
