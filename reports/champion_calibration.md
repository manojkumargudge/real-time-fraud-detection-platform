# Champion probability audit

The serving loader scored the unchanged XGBoost bundle
`baseline-9fdad27ea3b0` on its original chronological validation set.
The run checked the split against the bundle metadata and reproduced its
stored PR-AUC. The report uses 118,108 transactions, including 4,064 frauds.

| Measure | Result |
| --- | ---: |
| Validation start, TransactionDT | 12,192,900 |
| PR-AUC | 0.2992383060 |
| Brier score | 0.0281404390 |
| Log loss | 0.1146652843 |
| Expected calibration error, 10 equal-width bins | 0.0060768141 |

Run `python -m scripts.audit_champion_calibration` from the checkout with the
[IEEE-CIS inputs](../data/README.md) and the verified champion under
`artifacts/baseline`. The output includes the count, mean probability and
observed event rate per occupied bin. The lowest bin contains 111,404 rows
with mean probability 0.01561 and observed fraud rate 0.02021. Reporting the
bin counts matters because the overall error is weighted by bin size.

This validation audit did not fit a calibration model, change the threshold
or promote a model. Calibration on an independently collected production
cohort remains unverified.
