# Drift monitoring and retraining trigger

`python -m src.monitoring.run_drift` is a standalone batch job (not part of the API); in
Minikube it runs as a CronJob (checks only). Every threshold lives in one file,
[configs/drift.toml](../configs/drift.toml). All values are required, unknown keys are rejected,
and the values were set before the first real run. Evidently 0.7.23 (current `Report` /
`Dataset` API), with telemetry switched off.

**Time.** "Now" is the latest `transaction_dt` in `scored_transactions`, never wall-clock
time. Every window below is in transaction time and is printed and stored with each run.

**What is compared**

- **Reference**: the last 30 days of the champion's training window, with features from the
  same point-in-time code as serving. Not the whole window: history features (prior counts,
  time since the previous transaction) depend on how much history exists. The cold-start early
  weeks would look like drift against any later window.
- **Current**: scored transactions in the chosen window. Features are recomputed with the same
  code (equal to what Feast served, docs/feature_store.md), and the scores are the ones
  production stored.
- **Reference scores**: the champion's recipe is refit on the training rows *before* the
  reference window, then scored on it. These scores are out-of-sample, like production.
  In-sample scores would be overconfident and would look like prediction drift by themselves.

**Checks and trigger** (any one fires)

| Check | Method | Fires when |
|---|---|---|
| Feature drift | Evidently `DataDriftPreset` on the model's input features: Wasserstein (numeric), Jensen-Shannon (categorical); a feature drifts at score >= 0.1 | share of drifted features > 0.5 |
| Prediction drift | Evidently `ValueDrift` on the score, PSI | PSI >= 0.2 |
| Delayed-label performance | PR-AUC of stored scores on labels in (cutoff - 7 days, cutoff], needing at least 30 frauds | PR-AUC < champion validation PR-AUC - 0.05 |
| Top-feature drift (added later, see below) | the same per-feature test and threshold, on the champion's top 5 features by XGBoost gain (read from the champion's booster on every run, not hardcoded) | any of them drifts |

**Why the top-feature rule exists.** It was added on 2026-10-24, *after* the first demo runs
(runs 1 to 3 below). Those runs showed that the share rule (more than half of 28 features) did
not fire on a synthetic 2x TransactionAmt shift: only 11 of 28 features drifted, although
TransactionAmt itself was flagged. A shift in one or two important features is invisible to a
dataset-level share. The new rule watches the features the model relies on most. Every other
threshold is unchanged. Each row also stores the top-5 list and the ones that drifted.

**The matured-label window is the same in every run, by design.** The delayed-label PR-AUC
uses labels in (cutoff - 7 days, cutoff], with cutoff = now - 30 days. "Now" is the latest
scored transaction, not the run's current window, so with the full validation stream this is
always days 146 to 153 (20,264 rows, 617 frauds), whatever window is checked for drift. It
answers "how is the model doing on the newest labels we are allowed to see", which does not
depend on the drift window.

**Label delay.** Chargebacks arrive weeks after a transaction. A label is used only if its
transaction is at least 30 days older than now: cutoff = now - 30 days, and a label exactly at
the cutoff is usable. `usable_labels` is the only place labelled rows are selected. Tests poison
every label newer than the cutoff and show that the results do not change.

**When it fires** (host only):

1. Train a challenger with the champion's recipe on every usable label before the holdout. The
   holdout is the last 7 days of usable labels, (cutoff - 7 days, cutoff].
2. Log it to MLflow and register it with alias `challenger`.
3. Score champion and challenger on the same holdout. Paired bootstrap of the PR-AUC
   difference: 1,000 resamples, seed 42, resampling whole days.
4. Move `champion` only if the lower bound of the 95% CI of (challenger - champion) is above
   the margin of 0.01. Both point estimates, the CI and the reason are logged.

The job **fails** if the champion's or the challenger's training window overlaps the holdout:
the comparison would then be in-sample.

`--force-retrain` (host only) runs steps 1 to 4 even when no check fired. The row is then
stored with `forced = true`, and the promotion rule and the `production` guard are unchanged.
It cannot be combined with `--no-retrain`.

**Challenger reuse.** Step 1 is skipped when the current `challenger` was trained on exactly the
planned training rows. The check compares the run's tags `train_end_dt` and
`train_data_sha256` (SHA-256 of the sorted TransactionIDs and labels, the window bounds, the
seed and the imbalance method) with the planned data. The job then logs `reused challenger vN`
and runs the same bootstrap comparison with that model, so repeated runs on the same labels
stop adding identical registry versions. Challengers registered before this guard (v7) have
no such tags, so the next real run trains one more time.

**Design trade-off**

- **The trigger is cheap and sensitive by design.** A false alarm costs one retrain (about
  73 s on this laptop), and a reused challenger costs nothing.
- **Production is protected by the bootstrap promotion gate, not by the trigger.** `champion`
  moves only if the 95% CI lower bound of the PR-AUC gain is above the margin, and
  `production` is never moved by the job.
- **Real-data result:** challenger +0.0053 PR-AUC, 95% CI [-0.0160, +0.0254], so the champion
  was kept.
- **Known limits:**
  - The share rule (more than half of 28 features) missed a 2x TransactionAmt shift.
  - The top-5-by-gain rule fires on borderline drift (card3 at 0.1041 vs the 0.1 threshold)
    and also misses that shift, because TransactionAmt is not in the top 5 by gain.
  - **Next step (not built):** rank features by their drift impact on the score instead of by
    gain.

**Storage.** Each run writes one row to PostgreSQL table `drift_reports`. The row holds:

- the windows, now and the label cutoff;
- the drifted-feature share and the top drifted features;
- the PSI and whether prediction drift was detected;
- the delayed-label PR-AUC;
- whether it triggered and why;
- the retrain run ID, the challenger version and the promotion decision (point estimates and
  CI);
- the path of the Evidently HTML report in `reports/drift/` (gitignored).

Runs on a shifted copy are marked `synthetic = true`, and their HTML title starts with
`SYNTHETIC=TRUE`.

**What is not automated, and why**

- `production` is never moved by the job. `set_alias` refuses it, and the alias is re-read
  after every promotion. Moving `champion` does not change what is served: serving loads
  `artifacts/baseline/`. Deploying a new model is a human step.
- **Retraining runs on the host only.** The MLflow registry is a local SQLite file whose
  artifact paths are Windows `file:///C:/...` paths, which a Linux container cannot open. The
  Compose service therefore runs drift checks only:
  `docker compose --profile monitoring run --rm drift [--window-start-day D --window-end-day D]`.
  The fix is an MLflow server with a shared artifact store, which is out of scope here.
- Scheduling: in Minikube the drift CronJob runs every 6 h with `--no-retrain` (checks only;
  docs/kubernetes.md "Drift CronJob"). The Compose service is one-shot. A run that can retrain
  is started by hand on the host.
- The reference scores come from a fresh XGBoost refit, which is not bit-identical across
  platforms. On the same window, host and container gave the same drifted share (0.179) but a
  PSI of 0.0034 vs 0.0050 (drift_reports ids 1 and 4). Container rows store the container path
  of the report (`/app/reports/drift/...`, mounted to `reports/drift/`).

```
# host, with retraining (uses the MLflow champion):
set DATABASE_URL=postgresql://fraud:<password>@localhost:15432/fraud
.venv\Scripts\python -m src.monitoring.run_drift --window-start-day 141 --window-end-day 145
```

**First demo (2026-10-24, real data, before the top-feature rule).** All runs used the full validation stream in
`scored_transactions` (118,108 events, so now = day 183.0 and the label cutoff = day 153.0). The
config was unchanged between runs.

- Windows: champion training = days 1.00 to 141.12; challenger training (planned) = days 1.00
  to 146.00; holdout = (146.00, 153.00]; reference = days 111.1 to 141.1.
- The delayed-label check is the same in every run: (146.0, 153.0], 20,264 rows, 617 frauds.

| Run | Current window | Drifted features | Top drifted | PSI (score) | Delayed PR-AUC | Fired |
|---|---|---|---|---|---|---|
| 1 | days 141-145 (9,747 rows) | 5 / 28 = 0.179 | DeviceInfo, uid_txn_count_24h, uid_txn_count_7d | 0.0034 | 0.3238 | no |
| 2 | days 170-183 (34,502 rows) | 8 / 28 = 0.286 | uid_txn_count_1h, uid_txn_count_24h, DeviceInfo | 0.0027 | 0.3238 | no |
| 3, **synthetic** (TransactionAmt x2 on a copy of run 2's window) | days 170-183 | 11 / 28 = 0.393 | uid_txn_count_1h, uid_txn_count_24h, TransactionAmt, uid_amt_to_prior_mean | 0.0097 | 0.3238 | no |

Thresholds: share > 0.5; PSI >= 0.2; PR-AUC < 0.2992 - 0.05 = 0.2492.

- **Real drift grows with time** (0.179 to 0.286 of features), mostly in the short-window
  history counts and DeviceInfo. It stays below the threshold.
- **The score distribution barely moves** (PSI below 0.01).
- **Performance on matured labels is above the champion's validation PR-AUC** (0.3238 vs
  0.2992).
- **The synthetic shift is detected per feature** (TransactionAmt and the amount ratio), but it
  does not reach the dataset-level threshold.

No run fired, so the retraining and promotion path did not run on the real registry. It is
covered end to end on synthetic data by the integration test.

**Second demo (2026-10-24, with the top-feature rule; every other threshold unchanged).** The
champion's top 5 features by gain are `card3`, `card6`, `R_emaildomain`, `addr2` and
`ProductCD`. TransactionAmt is not among them.

| Run (drift_reports id) | Window | Share | PSI | Drifted top-5 features | Fired | Retrain / promotion |
|---|---|---|---|---|---|---|
| 1 (5) | days 141-145 | 0.179 | 0.0034 | card3 | **yes** (top-feature) | challenger v3, kept champion |
| 2 (6) | days 170-183 | 0.286 | 0.0027 | R_emaildomain | **yes** (top-feature) | challenger v4, kept champion |
| 3 (7), **synthetic** x2 | days 170-183 | 0.393 | 0.0097 | R_emaildomain | **yes** (top-feature) | challenger v5, kept champion |
| 4 (9), `--force-retrain` | days 170-183 | 0.286 | 0.0027 | R_emaildomain | yes, `forced = true` | challenger v7, kept champion |

What the second demo shows:

- **The new rule fired on all four of these runs, including run 1, which is close to
  training.** In run 1 it fires on `card3` (Wasserstein 0.1041 vs threshold 0.1, just over the
  line), so it is noisier than the share rule. It does not fire on every window: in the
  2026-10-27 evidence run (on copies; docs/evidence/2026-10-27-drift-trigger/) the default
  window, days 176-183, did not fire, and days 170-182, chosen after that, fired on
  `R_emaildomain` without `--force-retrain`. The rule is not calibrated on drift-free periods.
- Windows in these tables are `[start day, end day)`: the end day is excluded, so days 170-183
  covers 170.0 up to 183.0 (the end of the data), and 170-182 is one day shorter.
- **In the synthetic run it fired on real drift in `R_emaildomain`, not on the 2x
  TransactionAmt shift**, because TransactionAmt is not a top-5 feature. The rule does not
  catch the shift it was added for.
- I did not change any threshold. Whether to keep the rule, raise its threshold or widen it is
  an open decision.

**Promotion (identical in every run: same training data, same seed).** On the holdout
(146.00, 153.00], 20,264 rows:

- challenger PR-AUC 0.3290 vs champion 0.3238;
- paired day-block bootstrap of the difference (1,000 resamples): mean +0.0053, 95% CI
  [-0.0160, +0.0254];
- the lower bound -0.0160 is below the 0.01 margin, so **the champion was kept**: `champion` =
  v2, `production` = v2.

Each fired run registered a new challenger version (v3 to v7; `challenger` = v7). Row 8 is an
earlier `--force-retrain` run, stored as `forced = false` because the trigger also fired. The
flag now means "the flag was given"; row 9 is the corrected run.

**Velocity features (days 141-145): checked, no feature bug.**

- An independent naive recount of `uid_txn_count_1h` / `_24h` from the raw file matches the
  drift job on 1,000 of 1,000 sampled rows.
- The scores production stored (Feast features) equal the champion's prediction on the drift
  job's batch features for all 9,747 rows (max difference 1.9e-9).
- The large late-window drift score is a heavy tail. Median, p90 and the share of rows with
  activity are almost unchanged, but the p99 of `uid_txn_count_1h` goes from 5 to 127 (mean
  0.45 to 3.07, std 1.1 to 18.2). The Wasserstein distance is scaled by the reference std
  (1.13), so a small set of very bursty uids dominates it. Which uids they are was not
  investigated.
