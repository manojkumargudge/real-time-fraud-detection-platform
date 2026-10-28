# GNN for fraud-ring analysis (batch only)

A heterogeneous GraphSAGE model on a transaction-entity graph, plus candidate fraud rings. It
runs offline only and never goes into `/score`. **Result: it loses to XGBoost v2, adds
nothing as an XGBoost feature, and a score blend is worse too**, so the served model is
unchanged.

Install (CPU only, separate from serving and CI): `pip install -r requirements-gnn.txt`
(torch 2.8.0+cpu, torch_geometric 2.8.1, pyg-lib 0.4.0+pt28cpu). torch 2.10.0 was tried first
but fails to load on Windows (WinError 1114 in `c10.dll`) whenever pandas is imported first,
because pandas ships an older `msvcp140.dll`; 2.8.0 loads in any order.

| Step | Command | Output |
|---|---|---|
| Train + evaluate | `python -m src.graph.train [--degree-limit N] [--report PATH]` | reports/gnn_checkpoint.md, MLflow `fraud-gnn` |
| GNN score as XGBoost feature | `python -m src.graph.stack --degree-limit 1000 --epochs 7` | reports/gnn_stacking.md, `artifacts/gnn/scores.parquet` |
| Fraud rings | `DATABASE_URL=... python -m src.graph.rings` | reports/fraud_rings.md, table `fraud_rings` |

## Graph design (`src/graph/build.py`, `src/graph/data.py`)

- **Nodes:** one `tx` node per transaction, and one node per distinct value of five entity
  types (`ENTITY_COLUMNS` in `src/graph/build.py`). Edges run tx -> entity and back; there
  are no entity -> entity or tx -> tx edges.

  | Node type | Source column | What it really is |
  |---|---|---|
  | `tx` | one per `TransactionID` | a transaction |
  | `card` | `card_id` (card1–card6) | a BIN-like card proxy, not a card (see below) |
  | `device` | `DeviceInfo` | a device model or OS string (`Windows`, `iOS Device`), not a device ID |
  | `p_email` | `P_emaildomain` | the purchaser's email provider |
  | `r_email` | `R_emaildomain` | the recipient's email provider |
  | `addr1` | `addr1` | a billing region code |

- **No account node.** IEEE-CIS has no account or customer ID. Elsewhere in the project
  `uid` = `card_id` + `addr1` (`src/features/entity.py`) stands in for "account": the history
  features, the fraud rings and the blend's entity variant all group by it. The GNN graph has
  **no `uid` node**. It sees the same information only through the separate `card` and `addr1`
  nodes and through the uid history features on each `tx` node.
- **Transaction features:** the 28 XGBoost inputs (`compare.feature_columns`).
  - The 9 raw numeric and 12 history features get a signed log1p, are standardised with
    fit-split statistics, and get a missing indicator.
  - The 7 categoricals become embeddings: 0 = missing, 1 = unseen, 2+ = value, with the
    encoder fit on the fit split.
  - There is no label, D1, raw time or ID among them.
- **Entity nodes:** no features of their own, just one learned vector per node *type*. So
  values that appear only in validation need no learned parameters.
- **Hubs:** an entity value linked to more than 1,000 *training* transactions is dropped. The
  limit is counted on training rows only.

Training graph, with 472,432 transactions before the validation cut-off (`TransactionDT` 12,192,900):

| Entity | Nodes / edges kept | Hub values dropped | Training tx with a value | Training tx keeping a link |
|---|---|---|---|---|
| card | 13,715 / 274,716 | 77 (e.g. `9500\|321\|150\|visa\|226\|debit` 11,109) | 100% | 58.1% |
| device | 1,634 / 23,825 | 5 (`Windows` 39,681, `iOS Device`, `MacOS`, `Trident/7.0`, `rv:11.0`) | 21.1% | 5.0% |
| p_email | 41 / 9,192 | 18 (`gmail.com` 182,692, `yahoo.com`, ...) | 84.4% | 1.9% |
| r_email | 51 / 7,935 | 9 (`gmail.com` 47,290, ...) | 24.1% | 1.7% |
| addr1 | 280 / 7,572 | 48 (`299.0` 37,304, ...) | 88.6% | 1.6% |
| any | | | | 62.2% |

The full graph adds the 118,108 validation transactions as nodes and edges, without labels
(590,540 transactions in total). The complete hub list is in reports/gnn_checkpoint.md.

**The card_id proxy finding.** `card_id` (card1–card6) is not a card. It is a coarse proxy:
the 77 values above the limit cover 197,716 training transactions (41.9%), and the largest
covers 11,109. Dropping them leaves 41.9% of transactions without a card link. Using them as
ring links would create fake rings, so rings use uid and device links only.

## Leakage rules and their tests (tests/test_graph.py, tests/test_rings.py)

| Rule | How | Test |
|---|---|---|
| Message passing sees only strictly earlier transactions, per seed, through any entity | PyG temporal sampling with `disjoint=True`; seed time = `TransactionDT - 1` (PyG keeps `time <= seed time`; times are whole seconds); entity nodes have time 0 | `test_temporal_subgraphs_are_strictly_earlier_per_seed` checks every node against its own seed (disjoint batch assignment), and that the sample includes 2-hop paths through all five entity types; `test_same_timestamp_transactions_are_not_sampled` |
| The training graph has no validation transactions | built from `TransactionDT < 12,192,900` only | `test_training_graph_and_subgraphs_never_contain_validation` (also over sampled subgraphs) |
| Validation labels are never used for training, early stopping or the threshold | the label array holds `-1` for validation rows; the validation labels are returned outside the graph and read only by `evaluate`; `train_model` refuses a graph with validation rows | `test_validation_labels_never_reach_the_graph`, `test_train_model_refuses_a_graph_with_validation_rows`, `test_changing_validation_labels_changes_nothing_in_training` (every validation label flipped gives identical scores) |
| Early stopping and the threshold use a time holdout inside the training window | fit = training rows before the last 20% of training time; holdout = that last 20% | `test_fit_holdout_validation_are_time_ordered` |
| Hubs are chosen from training rows only | degrees are counted on rows before the cut-off | `test_hubs_are_counted_on_training_rows_only_and_dropped` (validation rows can't turn a rare value into a hub) |
| Stacking features are never built from own or future labels | forward chaining over 3 contiguous time blocks; block k is scored by a GNN trained on blocks < k; block 0 gets no score | `test_forward_chaining_scores_each_block_only_from_earlier_blocks` |
| Reproducible | fixed seeds; torch's RNG is reseeded per epoch and in `predict` (pyg-lib samples with it) | `test_fixed_seed_training_is_reproducible` |

**Remaining lookahead:**
- **Fixed preprocessing:** the feature scaling and categorical vocabulary are fit once on the
  fit split, and reused for the holdout and validation. This is the same as XGBoost.
- **Graph shape:** validation transactions are scored in a graph that also contains later
  validation transactions. Temporal sampling excludes them from every subgraph, but entity
  *indices* (which values exist) cover the whole period. Entity nodes carry no per-value
  parameters or features, so this exposes no information.
- **Hub list:** the list is fixed from training-window degrees. A value that becomes a hub
  only during validation stays in the graph.

## Results (validation, 118,108 transactions, 3.44% fraud, same split as XGBoost)

**GNN alone: official validation PR-AUC = 0.2021** (seed 42, hubs dropped, the final GNN
trained on all training labels for 7 fixed epochs; reports/gnn_stacking.md). It was reproduced
on 2026-10-27 from clean `main` 0d73adb in MLflow run `c233ecd0` (`fraud-gnn`): its
`artifacts/gnn/scores.parquet` (590,540 scores, sha256 `041078eb4343…`) is bit-identical to
the Oct 23 file.

**Early exploratory runs, not reproduced** (Oct 23, early-stopped checkpoints;
reports/gnn_checkpoint.md, reports/gnn_checkpoint_hubs_kept.md). They predate the
`git_dirty` tag and were not rerun, so treat them as indicative only:

| Model | PR-AUC | P / R @1% | P / R @2% | P / R @5% | Training | Peak memory |
|---|---|---|---|---|---|---|
| GNN, hubs dropped (limit 1,000) | 0.1766 | 0.3570 / 0.1038 | 0.3000 / 0.1745 | 0.2116 / 0.3076 | 649 s, 8 epochs (best 6) | 1.5 GB |
| GNN, hubs kept (fan-out caps them) | 0.1730 | 0.3190 / 0.0928 | 0.2806 / 0.1631 | 0.2120 / 0.3081 | 1,450 s, 6 epochs (best 3) | 2.5 GB |
| XGBoost v2 (served) | **0.2992** | 0.5330 / 0.1550 | 0.4312 / 0.2507 | 0.2877 / 0.4181 | | |

- **Setup:** 2 hetero SAGE layers, hidden 64, fan-out 15 / 10 per edge type, Adam lr 1e-3,
  batch 1,024, early stopping on the training holdout (patience 2).
- **Hub variants:** keeping hubs cost 2.2× the training time and did not help.
- **Final GNN:** trained on all training labels for 7 fixed epochs, it scores PR-AUC 0.2021 on
  validation (the official number above).
- **No tuning:** none was done. The gap to XGBoost is about 0.10 PR-AUC (0.2021 vs 0.2992).

**GNN score as an XGBoost feature** (reports/gnn_stacking.md). XGBoost was trained exactly as
v2 (no class weight), with and without a forward-chained `gnn_score` column:

| Model | Seed 42 | Seed 7 | Seed 2026 | Mean | Spread |
|---|---|---|---|---|---|
| XGBoost v2 | 0.2992 | 0.2877 | 0.2935 | 0.2935 | 0.0115 |
| XGBoost + GNN score | 0.2855 | 0.2813 | 0.2812 | 0.2827 | 0.0043 |

**No gain.** The mean is 0.0108 *lower*, and lower on every seed. The feature is not kept, and
the served model is unchanged.

**Score blend** `(1 - w) * xgb + w * gnn` (`python -m src.graph.blend`, reports/gnn_blend.md).
`w` was chosen on the last training block with out-of-fold inputs only, and validation was
evaluated once per variant at that `w`:

| Variant | GNN input | Chosen w | Validation AP blend vs XGBoost v2 | Delta, 95% CI | MLflow run |
|---|---|---|---|---|---|
| transaction | the transaction's own GNN score | 0.50 | 0.2749 vs 0.2992 | -0.0241 [-0.0346, -0.0137] | `c1a15d8b` |
| entity | mean earlier GNN score of the transaction's uid | 0.45 | 0.2797 vs 0.2992 | -0.0192 [-0.0263, -0.0118] | `602bb83c` |

**Both blends are worse**, so the served model stays XGBoost v2. The weight rule ("largest `w`
within the margin") did not pick the best selection weight (entity: 0.20), and the selection
gain did not carry over to the full-data models. No other weights will be evaluated on
validation.

## Why the GNN loses on this data

1. **The features are already relational.** The 12 history features summarise each uid's and
   card's past (counts, windows, amounts, device and address changes) with exact
   point-in-time rules. Through card links, the GNN's 2-hop neighbourhood mostly sees the same
   history again, sampled and averaged.
2. **Weak, anonymized links.** In IEEE-CIS:
   - `card_id` is a BIN-like proxy, and `addr1` and the email domains are regions and
     providers.
   - `DeviceInfo` is a device model or OS string, not a device ID.
   - After hub removal, 37.8% of training transactions have no link at all, and emails and
     addr1 keep links for under 2%.
   - Keeping the hubs adds only generic neighbours.
3. **Tabular signal.** The strongest signal is in each transaction's own fields (amount,
   product, card attributes) and the history features. Gradient-boosted trees use those
   better than a small MLP inside a GNN.
4. **The stacking feature is noisy and partly missing.** The first time block has no score by
   design. The remaining scores come from GNNs trained on less data (one third and two
   thirds of the window). XGBoost gives the feature weight, and it hurts on validation.

## Fraud rings (`src/graph/rings.py`, reports/fraud_rings.md, table `fraud_rings`)

- **Links:** users (`uid` = card + addr1) are linked through shared `DeviceInfo` values.
  `card_id` is never a link.
- **Hub devices:** a device value links users only if 2–10 distinct uids share it (`Windows`
  is shared by 10,896).
- **Hub uids:** a uid takes part only with at most 3 distinct devices and 1,000
  transactions; 1,840 uids are excluded. uid is a proxy too, and without this rule a few busy
  uids bridged device groups into one ring holding 84.2% of ring users. Device limits alone
  did not fix it: even at 2 users per device, one blob held 29%.
- **Result:** 337 rings with at least 2 users, holding 1,378 users. The median ring has 3
  users; the largest has 38 users and 598 transactions (2.8% of ring users).
- **Ranking:** rings are ranked by the mean GNN score of their *scored* transactions. **16
  rings are unscored**: all their transactions fall in the first training block, which has
  no score by design. They're listed separately, not ranked.
- **Labelled fraud rate (evaluation only):** 19.83% in the top 20 ranked rings, against 3.50%
  overall and 4.06% across all rings. The top rings are small (2–3 users, mostly a few
  transactions), so this is weak evidence.
- **The dashboard table:** `fraud_rings` (created if missing) holds the top 20 ranked rings:
  rank, uid count, card count, transaction count, scored transactions, total amount, shared
  devices, mean risk, fraud rate, time span and uids.

## Limitations

- The GNN is worse than XGBoost on this data and is not used for scoring, alone, as a
  stacking feature or in a score blend.
- One seed for the GNN itself; the stacking spread covers XGBoost seeds only.
- Rings rest on device-model strings, a weak link, and on hand-picked limits (10 / 3 /
  1,000), chosen by looking at ring sizes, not labels. They are candidates for review, not
  evidence of fraud.
- Training is CPU-only: 10–25 minutes per model, and the full stacking run took 43 minutes.
