# Drift-trigger calibration on in-training-period windows (M4)

**What was run.** `python -m scripts.drift_calibration` (commit 7457172, clean tree) on
2026-11-12, 1,798 s, from a git worktree. It reads the raw IEEE-CIS data and the served v2
bundle (for the recipe) only; no database, MLflow store or registry was read or written. Raw
output: [../2026-11-12-drift-calibration/](../2026-11-12-drift-calibration/calibration.md)
(`calibration.json`, `calibration.md`, `calibration.log`).
That raw output was generated with the script's earlier title and summary wording; it is kept
unchanged as recorded. Since 2026-11-13 the script says "in-training-period windows" and
"fired on"; the numbers are the same.

**Windows.** 11 non-overlapping 7-day windows inside the served model's training period
(days 61-138; the period is days 1-141), fixed in code before the run (`plan_windows`). For each
window the drift job's logic is replayed back in time: reference = the 30 days before the window,
scored by the v2 recipe refit on the rows before the reference; the window itself scored by the
recipe refit on all rows before the window; top-5-by-gain features from that refit; the delayed
labels (labels 30-37 days before the window's last transaction) scored by the first refit. The
rules and thresholds are the frozen `configs/drift.toml` (sha256 `bd47526f…`), including the
1,000-row minimum.

## Per window

| Window (days) | Rows current / reference | Rule that fired | Drifted top-gain features | Delayed-label rows (days) |
|---|---|---|---|---|
| 61-67 | 23,476 / 89,399 | none | - | 23,467 (31-38) |
| 68-74 | 20,638 / 89,408 | none | - | 20,710 (38-45) |
| 75-81 | 20,437 / 89,336 | none | - | 20,333 (45-52) |
| 82-88 | 21,059 / 89,439 | none | - | 19,555 (52-59) |
| 89-95 | 28,004 / 90,944 | none | - | 23,002 (59-66) |
| 96-102 | 22,030 / 95,945 | top-feature drift | addr2, R_emaildomain | 21,021 (66-73) |
| 103-109 | 20,718 / 96,956 | top-feature drift | addr2 | 20,796 (73-80) |
| 110-116 | 20,980 / 96,878 | top-feature drift; performance (in-sample, see below) | card3 | 20,554 (80-87) |
| 117-123 | 22,376 / 97,302 | top-feature drift; performance (in-sample) | card3 | 26,933 (87-94) |
| 124-130 | 21,717 / 92,746 | performance (in-sample) | - | 23,006 (94-101) |
| 131-137 | 18,589 / 91,457 | none | - | 20,945 (101-108) |

Days are inclusive (day 61-67 = `[61, 68)` in the raw output). No window fired on the
drifted-feature share (largest share 0.214 vs threshold 0.5) or on prediction PSI (largest
0.0236 vs 0.2). Every window had far more than 1,000 rows.

## In-sample check of the performance-rule fires

All rows of every window, its reference and its delayed-label window have
`TransactionDT < 12,192,900`, the served v2's `train_end`, and v2 was refit on all 472,432 rows
before that point (`metadata.json` `split.train_rows`). So the delayed-label rows behind the 3
performance-rule fires (windows 110-116, 117-123, 124-130) **were in the served model's training
data**. They are labelled **in-sample** and **not counted as evidence** for the performance rule.
(The calibration itself scored them with a refit trained on earlier rows, not with v2; the
label follows the served model.)

The top-feature rule compares feature distributions of the window with its reference and does
not use model scores on those rows; its 4 fires (windows 96-102 to 117-123: addr2 twice,
card3 twice, R_emaildomain once) are reported as measured.

## Result

- The trigger fired on **5 of 11 in-training-period windows** (days 96-130), with no injected
  shift: top-feature drift in 4 windows, the performance rule in 3 (in-sample, not counted);
  window 124-130 fired on the performance rule only.
- The share and PSI rules never fired on these windows.
- **No threshold or rule change** follows from this run (decided before it); the scope fixed
  the rules. The result is recorded as a known limitation: README "Known limitations" and
  docs/brief-evidence.md step 6.
