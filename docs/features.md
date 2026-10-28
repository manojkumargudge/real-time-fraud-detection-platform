# Feature Engineering (Milestone 2)

Code: [`src/features/`](../src/features). Run on the real data with
`python -m src.features.build`, which writes aggregate statistics only to
[`reports/feature_metrics.json`](../reports/feature_metrics.json).

## 1. Entity proxy (`entity.py`)

IEEE-CIS has **no customer or account ID**. Two proxy keys are built from anonymised fields:

| Key | Components | Used for |
|---|---|---|
| `card_id` | `card1, card2, card3, card4, card5, card6` | device / location change |
| `uid` | `card_id` + `addr1` | velocity, amount features |

Keys are strings such as `1111|321|150|visa|226|debit|315`. Integral floats print without `.0`.

Rules for missing values (deterministic):

- If `card1` is missing, the key is NaN. The row has no entity, and every history feature is NaN.
  (No such rows exist in the train file.)
- If any other component is missing, it becomes the token `<NA>`. Such rows share a coarser key
  with the same remaining fields.

**Limitation (measured on train, 590,540 rows):** the proxy is coarse. There are 43,018 `uid`s
and 14,893 `card_id`s. The largest `uid` has 9,900 rows, 17.9% of rows belong to `uid`s with more
than 1,000 transactions, and 17,358 `uid`s appear only once. `card1–6` behaves like an issuer or
BIN-level group, not one physical card, so one key mixes many customers. The reverse also
happens: one customer can split across keys when `addr1` is missing or changes.

### 1a. Candidate finer proxy: estimated account-start day (opt-in, experimental)

`account_start_day = TransactionDT // 86400 − D1`. `card_d1` and `uid_d1` append this value
to `card_id` and `uid`. Code: `build_account_start_keys` in `entity.py`. The comparison is
reproducible with `python -m src.features.proxy_report`, which writes
[`reports/proxy_comparison.json`](../reports/proxy_comparison.json). It is **not** used by the
default features.

**What D1 is, and what we assume.** D1 is an anonymised column. The provider describes the D
columns only as "timedelta, such as days between previous transaction". We do *not* treat D1 as
a date. We use it as "days since the card/account was first seen" only because the measurements
below are consistent with that reading. The resulting start day is a relative day index, not a
calendar date.

**Checks on D1 (train file).**

| Check | Result |
|---|---|
| Missing | 0.215%, present in every 30-day block (max 1.14% in block 4) |
| Values | whole numbers, 0–640 |
| D1 = 0 | 47.4% overall. By product: H 94.8%, R 88.6%, C 72.6%, S 41.3%, W 36.6% |
| Day-boundary shift | `start = (DT − h·3600) // 86400 − D1` is most stable within `uid` at h = 0. All 1–23 h shifts score lower (training window only). |

**Measured comparison (590,540 rows).** "Null" means D1 shuffled within the base entity: it
splits groups at least as finely, but with no time alignment.

| Proxy | Entities | p50 / p90 / p99 size | Max | % rows in groups >100 / >500 / >1000 |
|---|---|---|---|---|
| `card_id` | 14,893 | 4 / 41 / 674 | 14,112 | 76.86 / 57.62 / 45.87 |
| `uid` | 43,018 | 2 / 21 / 187 | 9,900 | 52.18 / 28.25 / 17.88 |
| `card_d1` | 154,233 | 2 / 8 / 32 | 1,414 | 2.18 / 0.24 / 0.24 |
| `uid_d1` | 222,493 | 1 / 6 / 20 | 1,414 | 0.87 / 0.24 / 0.24 |
| `uid_d1` null | 398,173 | 1 / 2 / 8 | 400 | 0.30 / 0.00 / 0.00 |

Purity on fields that are not part of the key: the share of rows matching their group's most
common value, in groups with at least 2 rows. Higher means the groups look more like single
accounts.

| Proxy | `P_emaildomain` | `DeviceInfo` |
|---|---|---|
| `card_id` / `uid` | 0.507 / 0.549 | 0.455 / 0.504 |
| `card_d1` / `uid_d1` | 0.755 / 0.841 | 0.634 / 0.709 |
| `card_d1` null / `uid_d1` null | 0.591 / 0.660 | 0.571 / 0.637 |

The candidate beats its null control even though the null is more fragmented. That is evidence
that D1 carries real account structure, not just extra splitting.

**Change flags.** The first two columns are the change rate among non-null flags.

| Flag | `card_id` | `card_d1` | `card_d1` null | Null rate `card_id` → `card_d1` |
|---|---|---|---|---|
| addr1 changed | 0.532 | 0.310 | 0.657 | 0.134 → 0.345 |
| DeviceInfo changed | 0.610 | 0.488 | 0.610 | 0.813 → 0.879 |
| DeviceType changed | 0.353 | 0.283 | 0.357 | 0.776 → 0.850 |
| addr2 changed | 0.0007 | 0.0005 | 0.0006 | 0.134 → 0.345 |

At `uid` level, the DeviceInfo change rate falls from 0.581 to 0.406 with `uid_d1` (null 0.539).

**Interpretation and limits.**

- The candidate cuts change rates below its null. Collisions between customers therefore
  explain much of the default flags' high rate.
- A 31–49% change rate is still implausible as real behaviour, so even `*_d1` flags must not be
  read as "the customer changed device/address". DeviceInfo strings also vary with OS versions.
- Coverage drops: more rows have no earlier history, so more flags are null.
- In the training window, fraud is *lower* when a flag is 1 than when it is 0, for every proxy
  (e.g. `card_d1` DeviceInfo: 5.6% vs 9.4%). The exception is addr2. This is descriptive only;
  whether the flags help a model is for Milestone 3 to test.
- Products H, R and C mostly have D1 = 0, so their start day equals the transaction day. Their
  `*_d1` history fragments by day: only 21.6% of groups that begin with D1 = 0 reappear on a
  later day.
- 39% of rows have a start-day group exactly ±1 day away within the same `uid`. This could be
  day-rounding fragmentation or genuinely separate accounts; the data cannot tell them apart.
- One `*_d1` group (1,414 rows across 35 days, D1 never missing) is still unusually large.

**Leakage.** The start day uses only the row's own `TransactionDT` and `D1`. Tests show it is
unchanged by removing other rows, removing future rows, or changing labels. Whether the provider
computed D1 with hindsight cannot be verified. The online path must receive D1 on each event,
with the same meaning.

**Decision.** The default features stay on `card_id`/`uid`. `*_d1` remains an experimental,
opt-in candidate. The model comparison in §1b supports it on PR-AUC but not on alert-level
metrics, and the point-in-time status of D1 is unresolved.

### 1b. Model comparison: baseline vs D1-based keys

Code: `src/models/compare.py` (primary run, seed 42) and `src/models/robustness.py` (seeds
1–3). Both write aggregate reports to [`reports/model_comparison.md`](../reports/model_comparison.md),
[`model_comparison.json`](../reports/model_comparison.json) and
[`model_comparison_seeds.json`](../reports/model_comparison_seeds.json).

**Setup**

- **Variants:** both use an identical feature list: raw transaction fields, train-fitted
  categorical codes and the 12 history features. They differ only in which keys the history
  features use: `card_id`/`uid` (baseline) or `card_d1`/`uid_d1` (candidate). Raw D1,
  `TransactionDT`, IDs and keys are never model inputs.
- **Split:** one shared chronological split.
  - Train: 472,432 rows, `DT < 12,192,900`.
  - Validation: 118,108 rows, prevalence 3.44%. Both variants scored the same rows, verified
    by an ID digest. No validation rows are dropped; NaNs go to XGBoost as missing.
- **Training:** XGBoost 2.1.4 (native API, `hist`). `scale_pos_weight` comes from training
  labels (27.46).
  - Stage 1: early-stop on a chronological holdout, the last 20% of training time. The alert
    threshold maximises F1 on that holdout.
  - Stage 2: refit on all training rows with the stage-1 round count, then apply the threshold
    unchanged.
- **Label isolation:** validation labels are used only to compute the metrics. A test permutes
  them and checks that thresholds, rounds and scores stay bit-identical.

**Results** (validation set). "Seed" is the XGBoost random seed; each PR-AUC cell is
baseline → candidate.

| Seed | PR-AUC | Δ PR-AUC (day-block bootstrap 95% CI) | Recall | Precision | F1 |
|---|---|---|---|---|---|
| 42 | 0.2504 → 0.2685 | +0.0182 [+0.0067, +0.0306] | 0.419 → 0.351 | 0.264 → 0.307 | 0.324 → 0.328 |
| 1 | 0.2566 → 0.2689 | +0.0122 [+0.0033, +0.0211] | 0.401 → 0.323 | 0.287 → 0.331 | 0.335 → 0.327 |
| 2 | 0.2588 → 0.2723 | +0.0135 [+0.0039, +0.0240] | 0.384 → 0.395 | 0.302 → 0.288 | 0.338 → 0.333 |
| 3 | 0.2536 → 0.2726 | +0.0190 [+0.0117, +0.0279] | 0.386 → 0.390 | 0.289 → 0.285 | 0.331 → 0.330 |

Seed 1–3 intervals use 100 resamples; seed 42 uses 500. Two independent runs of seeds 1–3
produced identical numbers.

**Reading**

- **PR-AUC:** the candidate ranks fraud better in all four seeds. The gain is +0.012 to +0.019,
  and every bootstrap interval excludes 0.
- **Alert level:** at the training-selected threshold there is no consistent gain. F1 is within
  ±0.01. Recall falls in two seeds and rises slightly in two. The share of validation
  transactions flagged ranges from 3.4% to 5.5%, so recall and precision move mainly with where
  the transferred threshold lands.
- **Importance:** in the candidate model, the `uid_txn_count_*` velocity features rank among
  the top gain features. In the baseline, they do not.

**Limitations of this comparison**

- **D1's meaning is undocumented.** If the provider computed D1 with hindsight, part of the gain
  could be future information. That cannot be ruled out from the data, so this is evidence of
  usefulness, *not* of point-in-time safety.
- **One validation window** of 42 days. The day-block bootstrap captures day-to-day variation
  but not entity correlation, and not other time periods.
- **Customer overlap.** Many validation entities also appear in training (see §7), which may
  inflate both variants' scores.
- **Threshold transfer.** The threshold is selected with the stage-1 model and transferred to
  the refit model, whose score scale can differ. That is likely why alert-level metrics
  fluctuate.
- **No tuning.** Hyperparameters were fixed, not tuned; a tuned model could change the gap.
- **Runtime.** Each seed took about 1.5–3 minutes on 12 CPU threads; the seed 1–3 timings were
  inflated by a concurrent duplicate run.

## 2. Point-in-time rule (`history.py`)

## 2. Point-in-time rule (`history.py`)

A row at time `t` uses **only rows of the same entity with `TransactionDT < t`**.

- **Tied timestamps:** rows with the same timestamp never see each other. The result does not
  depend on input row order (tested).
- **No future rows:** appending later rows never changes earlier features. This is tested on
  synthetic data and on the real file (first 70% of time vs full).
- **No labels:** labels are never read. Flipping or dropping `isFraud` leaves the output
  unchanged (tested).
- **Last known value:** where a "last known value" is needed, ties among earlier rows are broken
  by `TransactionID`.
- **Implementation:** sort by (entity, time, ID), then vectorised `searchsorted` on int64
  composite keys. This is O(n log n) on the CPU and takes about 5 s for the full train file on
  this machine.

## 3. Feature definitions

| Feature | Definition | No history | Missing entity |
|---|---|---|---|
| `uid_prior_txn_count` | count of earlier `uid` transactions | 0 | NaN |
| `uid_txn_count_{1h,24h,7d,30d}` | earlier transactions with `t - w <= DT < t` | 0 | NaN |
| `uid_seconds_since_prev_txn` | `t` − latest earlier timestamp | NaN | NaN |
| `uid_prior_amt_mean` | mean `TransactionAmt` of earlier transactions (current one excluded) | NaN | NaN |
| `uid_amt_to_prior_mean` | `TransactionAmt / uid_prior_amt_mean` (NaN if the mean is 0) | NaN | NaN |
| `card_{deviceinfo,devicetype,addr1,addr2}_changed` | 1 if the value differs from the card's last *known* earlier value, 0 if equal, NaN if either value is missing | NaN | NaN |

Change flags use `card_id`, because `addr1` is part of `uid` and an address change would start a
new `uid`. A missing current value is never counted as a change.

All features are float32. `uid_seconds_since_prev_txn` loses whole-second precision above 2^24 s
(about 194 days). That cannot happen within the 182-day train file, but it could on train+test.

## 4. Chronological split (`split.py`)

- **Rule:** train is `DT < train_end` and validation is `DT >= train_end + gap_seconds`.
- **Choosing `train_end`:** by default it is the timestamp at the `(1 − valid_fraction)` quantile.
- **Tied timestamps:** rows with the same timestamp always fall on the same side.
- **The gap:** rows inside the gap are dropped. A gap can simulate label delay, since chargebacks
  arrive weeks later. The default is 0 and is still to be decided.

History features are computed once over the full file **before** splitting. This is leak-free
because of §2: training rows cannot see validation rows, and validation rows see the history a
live system would have had. No labels are used.

## 5. Train-only preprocessing (`preprocess.py`)

`CategoricalEncoder` learns its vocabulary from training rows only:

- known values → `0..k-1` (sorted, so codes are deterministic)
- unseen or rare values → `-1`
- missing → NaN

Its state can be serialised to JSON so that serving can apply the same mapping. Frequency and
target encodings fitted over the whole training window are **deliberately excluded**: they leak
later activity, or labels, into earlier rows.

## 6. Measured results (train file, `valid_fraction=0.2`, `gap_seconds=0`)

| Metric | Value |
|---|---|
| Rows train / valid | 472,432 / 118,108 |
| `train_end` | 12,192,900 s |
| Fraud rate train / valid | 3.514% / 3.441% |
| Valid rows whose `uid` was unseen in train | 4.86% |
| Valid rows with no prior `uid` history | 3.33% |
| Valid rows with a `DeviceInfo` value unseen in train | 0.32% |
| `card_addr1_changed` mean (non-null, train) | 0.539 |
| `card_deviceinfo_changed` mean (non-null, train) | 0.619 |
| `card_addr2_changed` mean (non-null, train) | 0.0008 |

## 7. Known limitations and risks

The audit in [`audit_milestone2.md`](audit_milestone2.md) adds measured entity overlap and an
identifier-input ablation: validation PR-AUC falls from 0.2504 to 0.2107 without the raw card
and address codes.

- **Coarse proxy (§1).** The high change rates (54% address, 62% device) mostly reflect several
  customers sharing one `card_id`, not real behaviour changes. These flags may carry little
  signal. That is a hypothesis for Milestone 3, not a finding.
- **Entity overlap and label propagation.** 95% of validation rows have a `uid` that was seen in
  training. Kaggle labels every transaction of a card after a chargeback, so entity-level
  memorisation may inflate validation scores. Report this alongside results, and consider an
  entity-disjoint check.
- **Label maturity.** With `gap_seconds=0`, training labels near the cutoff assume chargebacks
  are known instantly.
- **`TransactionDT` is a relative offset.** Windows assume it is in seconds, which is the
  conventional reading but is not confirmed by the data provider.
- **Online and Feast parity (done).** The online Feast path and the Feast offline training
  matrix reproduce §2 exactly, including the tied-timestamp rule (same-second events do not see
  each other): docs/feature_store.md, full-file proof of 2026-10-28 (identical sha256).
- **Peak memory not measured.** It was not measured for this pipeline: the Windows venv launcher
  hides the child process from the method used.
