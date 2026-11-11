# Brief audit evidence (2026-11-11)

Captured by running on `main` ca83260 (after PRs #35 and #36). Read-only on the demo database
and the real `mlflow.db` (opened with SQLite `mode=ro`; sha256 `dd198df9…` unchanged).

| Step | File | What was run | Result |
|---|---|---|---|
| S1 class imbalance | `s1_fraud_rate.txt` | `load_ieee_cis` on the raw training file | 590,540 rows, 20,663 fraud, **3.4990%**; training 3.5135%, validation 3.4409%; daily 1.10%-6.99% |
| S2 train/serve parity | `s2_parity_tests.txt` | `pytest -v` on the Feast parity tests (`test_feast_store.py`, `test_entity_state.py`, `test_feast_training.py`) | **16 passed** |
| S3 MLflow runs | `s3_mlflow_runs.txt` | every run per experiment from `mlflow.db` | 44 active runs (fraud-xgboost 16, fraud-gnn 11, fraud-evaluation 8, fraud-drift 9); class weights vs none: `scale_pos_weight` mean valid PR-AUC **0.2517** vs `none` **0.2935** over 3 seeds; W1 0.2212 vs 0.2349, W2 0.2528 vs 0.2958 |
| S4 graph + blend | `s4_graph_nodes.txt` | `build_graph` + `to_hetero` (PyG 2.8.1) on the real data, default and `--graph account` | node types tx, card (14,816), device (1,781), p_email, r_email, addr1, and account (40,392) in the account graph; 4 blend runs in MLflow, all worse than XGBoost v2 (CI below zero) |
| S5 streaming on the bench copy | not in this commit | | see "Remaining" |
| Load test | [../2026-11-11-load-test/](../2026-11-11-load-test/README.md) | 3 runs, c=1 and c=5, 60 s | c=1 24-29 req/s, p50 31-34 ms; c=5 12.5-14.3 req/s; README's 2026-10-30 figures not reproduced |

## Remaining (not yet captured in this folder)

- S2 live evidence: consumer log lines with `features=feast` (captured 2026-11-11 in the
  gitignored `reports/bench/s5-2026-11-11.consumer-*.log`, 200 of 200 events); an API-side live
  log is not captured yet.
- S5 (200 events through producer -> Kafka -> 3 consumers on `fraud_bench`: 200 stored, 0
  duplicates, DLQ 0, lag 0; raw output in gitignored `reports/bench/s5-2026-11-11.*`) still to be
  copied here.
- S6 rerun on `main` (the Phase 2 run is in docs/evidence/2026-11-11-retrain-feast/), S7
  dashboard + Minikube, README/CLAUDE.md/brief-evidence updates, the 118,109-offset explanation,
  and the full test and scanner pass.
