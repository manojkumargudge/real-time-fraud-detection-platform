# Results and experiments

Moved from the README on 2026-10-28; numbers unchanged.

## Class imbalance

- Removing class weighting (`scale_pos_weight` 27.46 -> none) raised mean validation PR-AUC
  from 0.2517 to 0.2935 over 3 seeds; the gain (+0.0418) is about 3 times the seed spread
  (0.0134).
- Confirmed on two other time windows with seed 42: W1 +0.0137 (small), W2 +0.0430.
- Trade-off: on W1 at the 1% alert budget, `none` was slightly worse; it was better at 5%.
- Deployed on 2026-10-23: version 2 is now `production` and served by the API and consumer
  (`/health` reports `baseline-9fdad27ea3b0`).
- Details: [reports/imbalance_comparison.md](../reports/imbalance_comparison.md). Rerun with
  `python -m src.models.imbalance_comparison` and
  `python -m src.models.imbalance_comparison --confirm`.

## Served model v1 vs v2 at fixed alert budgets

Same validation window (118,108 rows, 3.44% fraud); at each budget both models flag the same
number of transactions ([reports/model_v1_vs_v2.md](../reports/model_v1_vs_v2.md),
`python -m src.models.version_comparison`):

| Budget | Recall v1 | Recall v2 | Precision v1 | Precision v2 | Threshold v1 | Threshold v2 |
|---|---|---|---|---|---|---|
| 1% | 0.1321 | 0.1550 | 0.4543 | 0.5330 | 0.8967 | 0.3640 |
| 2% | 0.2266 | 0.2507 | 0.3898 | 0.4312 | 0.8401 | 0.2433 |
| 5% | 0.3991 | 0.4181 | 0.2746 | 0.2877 | 0.6922 | 0.1121 |

Where v2 is worse: at its own deployed threshold (0.1281) it flags 4.31% of transactions and
catches 39.30% of frauds, against v1's 5.47% and 41.93% at 0.6721; and on window W1 at the 1%
budget (confirmation above). Thresholds are chosen per model, by max F1 on the training
holdout, so the alert rate changed with the model.

Known limitation: the threshold is chosen by max F1 on the stage-1 model's holdout scores but
applied to the refit model (trained on all training rows), so the deployed alert rate can differ
from the holdout's. Next step: an alert-budget threshold, checked on post-deployment data.

Note: the IEEE-CIS fraud rate is about 3.5% (3.499% overall), not under 1%.

## Why Feast is the default: train/serve skew

The store path only knows events the service has ingested. With a stream that starts at the
validation cut-off (as in production, after training), 96% of streamed rows get history
features that differ from training, validation PR-AUC falls from **0.2992 to 0.2533**, and
**2% of alert decisions flip** (2,345 of 118,108). Feast starts from the training history, so
its features equal the training features. The cost is latency: features + score take p50
107.5 ms with Feast vs 61.1 ms with the store path (p95 179.0 vs 88.8 ms), because of Feast
Python SDK overhead (a Redis round trip is 2 ms); see docs/feature_store.md.

## Demo: unseen validation data (reset 2026-10-23)

The demo stack streams **only validation-period transactions** (`TransactionDT` >=
12,192,900), in time order; the producer refuses anything earlier or out of order, and labels
are never sent. Feast was materialized from the 472,432 earlier transactions (39,087 uid and
13,792 card states). Earlier demo data was backed up and cleared.

| | First 5,000 events, 1 consumer | Next 5,000 events, 3 consumers |
|---|---|---|
| Throughput while a backlog existed | 7.3 events/s | 12.0 events/s (1.6x, not 3x) |
| Per-event latency in the consumer, p50 / p95 | 112.2 / 239.8 ms | 214.3 / 422.2 ms |
| Peak sampled consumer lag (whole batch published at once) | 3,910 | 3,130 |
| Errors, dead-lettered events, consumer restarts | 0 / 0 / 0 | 0 / 0 / 0 |

1.6× with 3 consumers on one laptop; per-event latency doubled. Profiled on 2026-10-25 ("Consumer throughput" below): CPU oversubscription by XGBoost and per-event Feast overhead.

First 5,000 events: 248 alerts (4.96%), 107 of them true fraud (precision 0.4315, recall
0.4693; 228 frauds). This window (the first ~1.9 days of validation) has a higher fraud rate
than validation as a whole, 4.56% vs 3.44%, and with 228 frauds the recall's 95% margin is
about +-0.065, so these numbers flatter the model. **The model's real performance is the
full-validation figure at the deployed threshold: precision 0.3136, recall 0.3930** (4.31% of
transactions alerted). Serving added no error: live scores matched offline batch scores
exactly (max difference 9×10⁻¹⁰), with identical alerts.

Three consumers did not scale linearly: per-event latency doubled, which points to a shared
bottleneck; the 2026-10-25 profiling found it ("Consumer throughput" below). Events
are keyed by `card_id` (events without card1 have no card key and use `id:<TransactionID>`),
so each card's events stay in one partition, in order, and each card's Feast state has one
writer among the consumers. After the rebalance, the Redis state of 300 sampled streamed uids
(216 cards) equalled the state rebuilt from batch data, and their next unseen events got the
same features as batch.

Earlier (2026-10-22) the at-least-once path was checked on the first IEEE-CIS transactions:
2,300 messages = 2,000 unique `TransactionID`s + 300 duplicates, each stored once.

## Consumer throughput (2026-10-25)

Benchmark on a clone of the demo database (`fraud_bench`), with 3 consumers exactly as in the
demo (`FEATURE_SOURCE=feast`) and the same 4,000 held-out events
(`python -m scripts.bench_throughput`; details in docs/serving.md "Throughput benchmark").

| | Before | After |
|---|---|---|
| Unthrottled throughput, 3 consumers | 13.1 events/s | **46.8 events/s** |
| End-to-end latency at 5 events/s, produce -> `scored_at` (start of the DB write, before the Feast push and offset commit), p50 / p95 | 114 / 669 ms | 31 / 61 ms |
| Scorer latency under load, p50 / p95 | 203 / 357 ms | 48 / 105 ms |
| Consumer CPU under load | ~1.8 cores each | ~0.5 cores each |

**Cause:**
- PostgreSQL, Redis and Kafka were not the bottleneck.
- XGBoost used all 12 cores for every one-row prediction, so 3 consumers oversubscribed the
  CPU.
- The Feast scoring path rebuilt the full batch feature frame per event, only to overwrite it.
- The Feast SDK deserialised every feature view on each read.

**Fix:** single-threaded predict, the per-event work done once, and Feast feature views
resolved once. Delivery, model, features and feature source are unchanged. The 4,000 replayed
scores are bit-identical to the scores the demo stored.

## Consumer crash fix and chaos test (2026-10-25)

**The crash:**
- During the full-validation stream on 2026-10-24, all 3 consumers exited twice.
- Cause: an offset commit failed with the retriable Kafka errors `NOT_COORDINATOR` /
  `COORDINATOR_LOAD_IN_PROGRESS` (broker under heavy load). The consumer tolerated only
  rebalance errors and re-raised everything else.
- Docker restarted the consumers and nothing was lost or duplicated, but the stream stalled
  each time.

**The fix** (`src/streaming/consumer.py`):
- The retriable coordinator and timeout errors (and any error librdkafka flags as retriable)
  are retried 3 times (0.2 / 0.5 / 1 s).
- If the commit still fails, it counts as `commit_rejected` and the consumer carries on. The
  message is redelivered later and the `TransactionID` primary key deduplicates it
  (at-least-once).
- If commits keep failing for more than 30 s, an ERROR is logged every 30 s with
  `consecutive_commit_failures`. Other errors still stop the consumer. Details: docs/serving.md
  ("Offsets").

**Chaos test** (test stack only, `python -m scripts.chaos_kafka_restart [--mode restart|kill|pause]`):
- Each mode ran 500 events and disrupted test Kafka mid-stream: restart, kill + start, or a
  70 s pause.
- In every mode, all 3 consumers stayed alive, 500 of 500 rows were written, there were 0
  duplicate IDs, lag was 0 and the DLQ was empty.
- `commit_rejected` was 0, 0 and 3. The 3 in the pause run came from a rebalance and were
  redelivered and skipped as duplicates.
- None of the runs triggered `NOT_COORDINATOR` on a commit. librdkafka waits for the
  coordinator instead, so the new retry path is verified by unit tests only. Results:
  docs/serving.md ("Chaos test").
