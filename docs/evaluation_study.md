# Evaluation Study: Seen/Unseen Entities, Identifier Ablation, Label-Delay Gaps

- **Results:** [`reports/evaluation_study.md`](../reports/evaluation_study.md); every number is
  in `evaluation_study.json`.
- **Code:** `src/models/evaluation_study.py`, run with `python -m src.models.evaluation_study`
  (about 10 minutes, seed 42).
- **Status:** experimental. The serving model, its 28 features and the scoring path are
  unchanged.

## Design

**What is compared**

- **Variants:**
  - `baseline`: the current serving feature set.
  - `no_identifier_codes`: the baseline without the raw `card1/2/3/5` and `addr1/2` codes. It
    keeps the history features, which are computed per card/uid.
- **Validation rows:** fixed at the Milestone 2 cutoff: 118,108 rows, 3.44% fraud. Every gap
  and variant scores exactly these rows (one ID digest, checked at runtime).

**Gap g**

- Training uses `TransactionDT < valid_start − g`.
- Gap rows are excluded from both training and evaluation.
- Gap rows may still appear in a validation row's *feature history*: transactions are observed
  when they happen, and only their labels are delayed.
- Training-row features never use anything after `train_end`
  (`test_training_features_ignore_rows_after_train_end`).

**Training protocol** (same for every variant and gap, using training rows only)

- `compare.train_variant`.
- `scale_pos_weight` comes from training labels.
- Early stopping and the max-F1 threshold use a chronological holdout from the training window.
- The model is then refit on all training rows.
- Validation labels are used only for metrics, enforced by
  `test_validation_labels_do_not_change_training_outcomes`.

**Subgroups**

- "Seen" means the card or uid appears in *that model's* training rows.
- An unseen card implies an unseen uid, so "card and uid both unseen" is the same as
  `card_unseen`.
- `card_seen_uid_unseen` isolates known cards with a new `addr1`.
- No validation row lacks `card1`, so `no_entity_key` is empty.

**Uncertainty**

- PR-AUC intervals come from a 200-resample day-block bootstrap.
- Ablation deltas use a paired bootstrap on identical rows.
- These intervals reflect variation across validation days, not training randomness (one seed).

## Findings

### 1. Most validation traffic comes from known entities

At gap 0, 98.6% of validation rows have a card seen in training. Only 1,678 rows (62 frauds)
have an unseen card, and 5,742 rows (201 frauds) have an unseen uid.

| Baseline subgroup (gap 0) | Rows | Frauds | PR-AUC [95% CI] |
|---|---|---|---|
| uid seen | 112,366 | 3,863 | 0.255 [0.235, 0.276] |
| uid unseen | 5,742 | 201 | 0.154 [0.108, 0.221] |
| known card, new uid | 4,064 | 139 | 0.126 [0.086, 0.207] |
| card unseen | 1,678 | 62 | 0.240 [0.131, 0.397] |

The baseline ranks fraud clearly worse for new uids than for known ones. The card-unseen
interval is too wide (62 frauds) to support a firm comparison.

### 2. Identifier codes help known entities and hurt new uids

Restricted minus baseline PR-AUC, paired bootstrap:

| Subgroup | Gap 0 | Gap 7 | Gap 30 |
|---|---|---|---|
| all | −0.040 [−0.050, −0.032] | −0.035 [−0.045, −0.025] | −0.025 [−0.034, −0.015] |
| uid seen | −0.045 [−0.056, −0.036] | −0.038 [−0.049, −0.028] | −0.026 [−0.035, −0.016] |
| uid unseen | **+0.049 [+0.014, +0.092]** | **+0.035 [+0.003, +0.071]** | −0.004 [−0.045, +0.035] |
| card unseen | +0.043 [−0.000, +0.099] | +0.029 [−0.048, +0.108] | −0.141 [−0.238, −0.053] |

- **Known entities:** removing the codes lowers PR-AUC overall and for seen entities at every
  gap. The penalty shrinks as the gap grows.
- **New uids:** at gaps 0 and 7, the restricted model ranks new uids *better*, with intervals
  that exclude 0. This is consistent with the codes letting the model memorise known entities at
  the expense of generalising.
- **No effect at gap 30:** the new-uid advantage disappears (−0.004, interval includes 0).
- **Unseen cards are unstable:** the card-unseen result changes sign between gaps (+0.043 at
  gap 0, −0.141 at gap 30), with only 62–95 frauds. **No conclusion is drawn for unseen cards.**
- **Gap 60 is excluded:** the 60-day restricted model early-stopped at **6 rounds** (other
  models: 196–670), so the gap-60 ablation is unreliable (flagged ⚠ in the report).

### 3. Label-delay sensitivity

| Gap (days) | Train rows | Baseline PR-AUC | Restricted PR-AUC |
|---|---|---|---|
| 0 | 472,432 | 0.250 [0.230, 0.272] | 0.211 [0.189, 0.232] |
| 7 | 453,779 | 0.238 [0.218, 0.259] | 0.203 [0.185, 0.224] |
| 30 | 383,586 | 0.211 [0.194, 0.233] | 0.187 [0.171, 0.206] |
| 60 | 285,910 | 0.169 [0.156, 0.186] | 0.153 ⚠ (6-round model) |

- **Trend:** PR-AUC falls as the gap grows. This is a **sensitivity analysis, not a latency
  measurement**: nothing in this project's dataset documentation establishes chargeback
  latency, and the gaps are an assumed grid.
- **Confounded:** larger gaps also remove 4–39% of training rows and make the remaining data
  older. The drop cannot be attributed to label delay alone.
- **Practical reading:** the gap-0 headline (0.250) is an optimistic upper reference if labels
  in practice arrive days or weeks late.
- **Alert-level metrics are unstable:** the training-selected threshold transfers unevenly
  (baseline recall 0.42 → 0.43 → 0.29 → 0.21 across gaps), consistent with the earlier
  threshold-transfer finding.

## Recommendation

1. **Keep the serving baseline unchanged.** It has the best overall PR-AUC at every gap, the
   differences are significant at the day level, and about 95% of validation rows come from
   uids seen in training.
2. **The restricted variant merits further evaluation for new-entity traffic, not as a
   replacement.** Its advantage on unseen uids is measured on 201–203 frauds, one seed and one
   validation window, and it vanishes at a 30-day gap. Before any decision:
   - repeat across seeds and several chronological folds;
   - consider a routed or hybrid design (baseline for known entities, restricted for new
     ones), evaluated with an alert budget.
3. **Make round selection robust before more gap experiments.** Fixing the round count, or
   averaging over seeds, would avoid barely-trained models like the 6-round one. That is a
   protocol change, so it should be applied to every variant at once.

## Limitations

- **Single run:** one seed and one 42-day validation window. The intervals cover day-to-day
  variation, not training randomness or other time periods.
- **Small subgroups:** only 62–127 frauds have unseen cards. These subgroups pass the
  50-fraud reliability bar, but their intervals are wide and their results unstable.
- **Coarse proxies:** "seen" is defined on `card_id`/`uid`, which are coarse proxies (see
  `features.md` §1), not real customers.
- **Gap confound:** label delay is confounded with training-set size and data age, and the gap
  lengths are assumptions.
- **Kaggle label propagation** still inflates seen-entity performance for both variants.
- **Thresholds** are transferred from the stage-1 model, so precision and recall at the
  threshold are less stable than PR-AUC.
