# Milestone 2 Correctness Audit (2026-10-21)

**Scope:** temporal features, split, preprocessing, model comparison and the online feature
path. Out of scope: no code was rewritten and no new features were added. The audit added tests
where coverage was missing, plus one evidence script, `src/models/leakage_audit.py`, which
writes `reports/leakage_audit.json`.

**Statuses:**

- **VERIFIED:** the property is established by the cited code and tests, within the stated
  scope.
- **INCOMPLETE:** a risk was measured or documented, but not resolved.
- **BLOCKED:** cannot be settled without information this project does not have.

## Audit table

| # | Property | Status | Evidence (code → tests) | Scope / caveat |
|---|---|---|---|---|
| 1 | History uses only same-entity rows with `TransactionDT < t` | VERIFIED | `features/history.py` `_PointInTimeIndex` (`left = searchsorted(keys, keys, "left")`) → `test_features.py::test_matches_brute_force_reference` (O(n²) reference, many ties and NaNs), `::test_appending_future_rows_does_not_change_past_features`, `::test_real_data_features_are_point_in_time` (real file, first 70% of time vs full) | Exact equality, not approximate. |
| 2 | Counts, velocity windows, prior mean and amount ratio definitions | VERIFIED | same module → `::test_hand_computed_example`, `::test_window_lower_bound_is_inclusive` (window is `[t−w, t)`), `::test_current_amount_not_in_own_prior_mean`, `::test_zero_prior_mean_gives_nan_ratio` | — |
| 3 | Change flags compare against the last known *earlier* value | VERIFIED | `history._change_flags` / `last_known_before` → `::test_missing_device_is_not_a_change`, `::test_location_change_is_tracked_per_card_not_uid`, brute-force test | Rates are inflated by coarse proxies (`features.md` §1, §1a). |
| 4 | Equal timestamps never see each other; ties broken by `TransactionID` | VERIFIED | → `::test_duplicate_timestamps_do_not_see_each_other`, `::test_tie_break_uses_transaction_id_not_row_position`; online: `test_serving.py::TestParityEdgeCases::test_chronological_replay_matches_batch_on_edge_cases[by_id, reversed]` | Online parity holds even when tied events arrive in reverse ID order. |
| 5 | Results independent of input row order and index | VERIFIED | → `::test_input_row_order_does_not_matter`, `::test_output_aligned_to_non_default_index` | — |
| 6 | Missing timestamps rejected | VERIFIED | `history._validate`, `load.NON_NULL`, `serving/schema.py` (required field) → `::test_null_time`, `test_serving.py::TestSchema::test_required_fields[TransactionDT]` | — |
| 7 | Missing entity keys | VERIFIED | `features/entity.py` (`card1` missing → no entity; other parts → `<NA>`) → `::test_missing_card1_gives_nan_features`, `::test_missing_card1_rows_do_not_form_a_shared_history`, `::test_missing_addr1_rows_share_coarser_entity`; online: `TestParityEdgeCases` events 3, 4, 8 | A missing `addr1` merges rows into a coarser `uid`, by design. |
| 8 | Unseen entities and cold start | VERIFIED | → `::test_unseen_entity_has_zero_count_and_nan_history`; online: `TestParityEdgeCases::test_cold_start_has_no_history_until_replayed` | An empty online store gives count 0 even where batch history exists. That divergence is documented, not a bug. |
| 9 | Chronological split boundaries | VERIFIED | `features/split.py` → 7 tests in `test_features.py::TestChronologicalSplit` (strict separation, ties never straddle the boundary, gap, empty side); `test_models.py::TestComparison::test_shared_split_and_no_dropped_rows`; real data: `max_train_dt_lt_min_valid_dt = true` (`reports/feature_metrics.json`) | — |
| 10 | Entity overlap between train and validation documented | VERIFIED (documented) | `models/leakage_audit.py::entity_overlap` → `test_models.py::TestLeakageAudit`; numbers below | Overlap is large, so evaluation is optimistic (see #13). |
| 11 | No target leakage in model inputs | VERIFIED | `models/compare.py` `EXCLUDED` (`TransactionID`, `TransactionDT`, `isFraud`, `D1`, keys) → `test_models.py::TestFeatureFrames::test_variants_share_columns_and_exclude_unsafe_inputs`; labels never read by features: `::test_features_ignore_labels`; schema rejects `isFraud`: `TestSchema::test_label_d1_and_unknown_fields_rejected` | Direct and aggregation leakage only. Label propagation is covered in #13. |
| 12 | Validation labels never used for fitting, rounds or threshold | VERIFIED | `compare.train_variant` receives training rows only → `TestComparison::test_validation_labels_do_not_affect_training` (permuted validation labels → bit-identical threshold, rounds and scores), `::test_threshold_comes_from_training_holdout` | — |
| 13 | Identifier leakage / entity memorisation | **INCOMPLETE** | Raw `card1/2/3/5` and `addr1/2` are model inputs. Ablation (`reports/leakage_audit.json`, seed 42): validation PR-AUC **0.2504 → 0.2107** without them; recall 0.419 → 0.347 | They are pre-event attributes, not target leakage. But they let the model memorise cards whose chargeback labels Kaggle propagates, and with `gap_seconds = 0` they assume labels are known instantly. Not mitigated; this needs a decision (§3). |
| 14 | Inputs contain no post-event information | **INCOMPLETE** | Inputs are the transaction's own fields plus strictly-earlier history (#1). `has_identity`, `dist1`, `card*`, `addr*`, email domains and device fields are *assumed* to be captured at authorisation. | Provider semantics are undisclosed, so this cannot be verified from the data. |
| 15 | Categorical encoder fitted on training rows only | VERIFIED | `features/preprocess.py`; fit calls in `compare.train_variant` (stage 1 on fit split, stage 2 on train), `features/build.py`, `models/train.py` → `test_features.py::TestCategoricalEncoder::test_fit_on_train_only_and_unseen_maps_to_minus_one`; **new** `test_models.py::TestEncoderFitScope` (a validation-only category is absent from `train_variant` and the saved bundle vocabulary, and encodes to −1) | The label-permutation test (#12) could not catch feature-level fitting on validation; the new tests do. |
| 16 | Batch–online parity under chronological replay | VERIFIED | `serving/online_features.py` reuses `compare.build_model_frame` → `test_serving.py::TestTrainServeParity::test_online_features_and_scores_match_batch` (800 synthetic events, scores rtol 1e-6); **new** `TestParityEdgeCases` (ties, missing and coarse keys, unseen entity, 1 h boundary); **new** `test_real_data_online_replay_matches_batch` (first 300 real transactions: 16 tied rows, 40 missing `addr1`); `test_integration.py::test_scorer_on_postgres_matches_in_memory` | Requires every earlier event of the card to be ingested before the scored event. The real-data sample has shallow history. |
| 17 | Parity under arbitrary live arrival order | VERIFIED (no future data); parity **not** guaranteed | `store.card_history(..., transaction_dt < t)`, plus a second filter in `online_model_row` → `::test_future_events_in_store_are_never_used`, `::test_defensive_filter_drops_future_and_self`, `test_integration.py::...::test_card_history_is_strictly_earlier_and_per_card`; **new** `TestParityEdgeCases::test_late_arrival_diverges_from_batch_without_using_future_data` | A late event is missing from the history of events scored before it, so online ≠ batch for those events. Future data is never used. Kafka keying by card keeps per-card order *if* the producer sends in time order; that is a deployment assumption. |
| 18 | D1 proxy stays experimental | VERIFIED (kept out of serving); semantics **BLOCKED** | `models/train.py` trains baseline only; `serving/model.py` rejects other variants → `TestModelBundle::test_incompatible_metadata[variant…]`; schema rejects `D1` → `TestSchema`; evidence in `features.md` §1a–§1b | D1's meaning and point-in-time availability need provider documentation. See §2. |

## 1. Measured entity overlap (`reports/leakage_audit.json`)

| Proxy | Validation rows whose entity was seen in train | Validation entities seen in train | Validation rows whose entity had a **train fraud label** | Validation fraud rate: those rows / the rest |
|---|---|---|---|---|
| `card_id` | 98.58% | 85.27% of 7,474 | 74.28% | 4.15% / 1.39% |
| `uid` | 95.14% | 76.72% of 16,889 | 49.76% | 5.30% / 1.60% |

Rows whose `uid` already had a fraud label in training are 3.3× as likely to be fraud. This is
consistent with Kaggle's label propagation: once a card is charged back, its later transactions
inherit the label. It explains why identity-like inputs carry signal (#13).

## 2. Why the D1 keys remain experimental

- **Measured benefit:** PR-AUC improves by +0.012 to +0.019 on all four seeds, and every
  day-block bootstrap interval excludes 0 (`reports/model_comparison*.json`).
- **No alert-level benefit:** F1 at the training-selected threshold is within ±0.01, and recall
  falls in two of four seeds.
- **Undocumented semantics:** D1 is an anonymised "timedelta". "Days since the card or account
  was first seen" is an inference from data patterns, not a documented fact.
- **Product-dependent zeros:** D1 = 0 for 73–95% of rows in products H, R and C, so for those
  products it probably does not measure account age.
- **Unknown point-in-time availability:** if the provider computed D1 using later information,
  part of the gain would be leakage. The data cannot rule this out.
- **Not in serving:** D1 is excluded from model inputs, the serving schema rejects it, and the
  serving loader refuses non-baseline bundles.

**Unblocking requires:** documentation from the provider, or an independent data source with
true account-open dates.

## 3. Open decisions (not changed in this audit)

1. **Identifier inputs (#13):** decide whether to keep or drop the raw card and address codes.
   Then report validation separately for seen and unseen entities, and/or add a label-delay
   `gap_seconds`, so the headline PR-AUC is not dominated by memorised entities.
2. **Post-event fields (#14):** confirm with the data provider when `dist1` and the identity
   fields become available.
3. **Late events (#17):** if live arrival order matters, either re-score late-affected events or
   document the bounded divergence as an accepted operational limit.

## Reproduce

```
.venv\Scripts\python -m src.models.leakage_audit        # reports/leakage_audit.json (~75 s)
.venv\Scripts\python -m pytest -q                         # set TEST_* env vars for integration tests
```
