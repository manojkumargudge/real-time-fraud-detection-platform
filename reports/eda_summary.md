# EDA Summary — IEEE-CIS Fraud Detection

> **Status: measured on 2026-10-21.** Every number below comes from
> `python -m src.data.eda` on the Kaggle `train_transaction.csv` + `train_identity.csv`,
> recorded in [`eda_metrics.json`](eda_metrics.json). Figures come from
> `notebooks/01_eda.ipynb`, which was executed from a clean kernel; its final cell asserts that
> all metric sections equal `eda_metrics.json`. Data-quality checks are in
> [`data_quality.md`](data_quality.md).

Dataset: IEEE-CIS train split, transactions left-joined with identity on `TransactionID`.

## 1. Dataset overview and merge integrity

| Metric | Value |
|---|---|
| Transaction rows | 590,540 (394 columns) |
| Identity rows | 144,233 (41 columns) |
| Merged rows / columns | 590,540 / 435 (incl. `has_identity` flag) |
| Row count preserved by merge | yes |
| `TransactionID` unique (both files) | yes |
| Identity rows matched / orphaned | 144,233 / 0 |
| Rows already sorted by `TransactionDT` | yes |
| In-memory size, optimized dtypes | 1,536 MB |
| Peak process memory, full load + checks | 5,079 MB measured (`data_quality.json`) |
| Load + checks runtime (warm disk cache) | 46.3 s measured (`data_quality.json`) |

## 2. Class imbalance

| Metric | Value |
|---|---|
| Fraud transactions | 20,663 |
| Legitimate transactions | 569,877 |
| Fraud rate | 3.499% |
| Negative : positive ratio | 27.58 : 1 |

**Implication:** a model predicting "legit" for everything scores 96.5% accuracy, so accuracy is
not reported. Primary metric: PR-AUC (baseline for a random classifier ≈ 0.035), plus precision
and recall at a chosen operating threshold. XGBoost `scale_pos_weight` will start from the
measured training-window ratio (≈27.6, to be recomputed on the time-based training split) and be
tuned on validation. No SMOTE/resampling, to keep scores calibrated.

## 3. Identity coverage

| Metric | Value |
|---|---|
| Transactions with identity rows | 24.42% |
| Fraud rate with identity | 7.847% |
| Fraud rate without identity | 2.094% |

Identity presence is itself strongly associated with fraud (~3.7× the rate), so
`has_identity` is a meaningful signal rather than just a merge artefact.

## 4. TransactionDT distribution (fraud over time)

`TransactionDT` is a seconds offset from an undisclosed reference, not a calendar timestamp.

| Metric | Value |
|---|---|
| Range (seconds) | 86,400 – 15,811,131 |
| Span | 182.0 days (182 distinct day buckets) |
| Daily transactions min / median / max | 2,048 / 3,049.5 / 6,852 |
| Daily fraud rate min / median / max | 1.097% / 3.564% / 6.994% |

| 30-day block | Rows | Fraud rate |
|---|---|---|
| 0 | 134,339 | 2.532% |
| 1 | 89,399 | 4.001% |
| 2 | 92,189 | 4.040% |
| 3 | 98,615 | 3.953% |
| 4 | 83,571 | 3.410% |
| 5 | 86,934 | 3.419% |
| 6 (partial, 2 days) | 5,493 | 4.387% |

**Implication:** volume and fraud rate shift over time (block 0 has the most rows and the
lowest fraud rate). Splits will be time-ordered by `TransactionDT`, never random. The
training-window prevalence will differ from validation/test, which affects threshold choice and
is a natural first drift scenario for Evidently.

## 5. Missingness

| Metric | Value |
|---|---|
| Columns with no missing values | 21 of 435 |
| Columns > 50% missing | 214 |
| Columns > 90% missing | 12 |
| Most-missing columns | `id_24` 99.2%; `id_07/08/21/22/23/25/26/27` ≈ 99.1%; `dist2` 93.6% |

Mean missing fraction by column family: `C` 0.0%, `card` 0.5%, `addr` 11.1%,
`P_emaildomain` 16.0%, `V` 43.0%, `M` 49.9%, `D` 58.2%, `DeviceType` 76.2%, `dist` 76.6%,
`R_emaildomain` 76.8%, `DeviceInfo` 79.9%, `id` 84.8%.

**Missingness is informative** for several key columns (missing rate, legit vs fraud):

| Column | Legit | Fraud |
|---|---|---|
| addr1 | 10.17% | 37.46% |
| dist1 | 59.02% | 76.99% |
| R_emaildomain | 77.88% | 45.67% |
| DeviceInfo | 80.69% | 58.35% |
| P_emaildomain | 16.09% | 13.50% |
| card2 | 1.49% | 2.05% |
| D1 | 0.21% | 0.22% |

**Implication:** keep NaN as-is (XGBoost learns a default split direction) and do not impute
with global statistics; consider explicit missing-indicator features for `addr1` and
`R_emaildomain`.

## 6. Feature availability

Share of rows where the source columns are present.

| Planned feature | Source columns | All present | Any present |
|---|---|---|---|
| Transaction velocity | TransactionDT, card1 | 100.0% | 100.0% |
| Amount vs user average | TransactionAmt, card1, addr1 | 88.87% | 100.0% |
| Device change | DeviceInfo, DeviceType | 20.09% | 23.85% |
| Location change | addr1, addr2, dist1 | 40.33% | 88.89% |
| Email domain risk | P_emaildomain, R_emaildomain | 21.37% | 85.88% |
| Graph entities | card1, addr1, P_emaildomain, DeviceInfo | 11.63% | 100.0% |

**Implications:** velocity and amount features cover nearly all rows. Device-change features
will be sparse (≈24% of rows), so they must handle "no device history" explicitly. The graph
will be heterogeneous with partial edges: every transaction links to a card, but only ~12%
link to all four entity types.

## 7. Transaction amount by class (`TransactionAmt`)

| Class | p50 | p95 | p99 |
|---|---|---|---|
| Legit | 68.50 | 435.00 | 1,104.00 |
| Fraud | 75.00 | 500.00 | 994.00 |

Raw amount separates the classes weakly; relative amount (vs the user's history) is more likely
to help. This is a hypothesis to test in Milestone 2/3, not a finding.

## 8. Known limitations

- No true account/user ID: user-level features use a proxy built from card and address fields.
- `TransactionDT` is a relative offset; absolute dates, weekdays and time zones are unknown.
- Many columns (`C*`, `D*`, `M*`, `V*`, `id_*`) are anonymised; their meaning is not disclosed.
- Labels follow Kaggle's definition (a card is marked fraud after a reported chargeback, and
  subsequent linked transactions inherit the label), which may inflate user-level signal.
- Figures: [class balance](figures/class_balance.png),
  [amount distribution](figures/amount_distribution.png),
  [amount by class](figures/amount_by_class.png), [fraud over time](figures/fraud_over_time.png),
  [missingness by family](figures/missingness.png),
  [missingness by class](figures/missingness_by_class.png),
  [fraud rate by category](figures/categorical_fraud_rates.png). Category rates for small
  groups are noisy; for example, `card6` "charge card" has n = 15. Read rates together with
  the group sizes shown in the chart.
