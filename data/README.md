# Data

Datasets are **not** committed to Git (see `.gitignore`). Everything under `data/` except
this file is ignored.

## IEEE-CIS Fraud Detection

1. Accept the competition rules at https://www.kaggle.com/c/ieee-fraud-detection/rules
2. Download from https://www.kaggle.com/c/ieee-fraud-detection/data
3. Place the files at:

```
data/raw/ieee-cis/train_transaction.csv
data/raw/ieee-cis/train_identity.csv
data/raw/ieee-cis/test_transaction.csv   # optional, unlabeled
data/raw/ieee-cis/test_identity.csv      # optional
```

To use a different location, set `FRAUD_RAW_DATA_DIR`.

Verify with:

```
.venv\Scripts\python -m src.data.load          # rows, columns, fraud rate, identity coverage
.venv\Scripts\python -m pytest -m real_data     # loader + feature checks on the real files
```

The loader rejects files it cannot trust instead of loading them partially: missing files or
columns, empty or header-only files, malformed or truncated rows, non-UTF-8 content, invalid
labels and duplicate `TransactionID`s.

## Re-running the EDA

All outputs are aggregate-only; no rows are written.

```
.venv\Scripts\python -m src.data.eda       # reports/eda_metrics.json
.venv\Scripts\python -m src.data.quality   # reports/data_quality.{json,md} (timing, peak memory)
.venv\Scripts\python -m pip install -r requirements-eda.txt   # once, for the notebook
.venv\Scripts\jupyter nbconvert --to notebook --execute --inplace notebooks/01_eda.ipynb
```

The notebook writes `reports/figures/*.png` and fails if its metrics differ from
`reports/eda_metrics.json`. A full load peaks at about 5 GB of RAM.

Usage is subject to the Kaggle competition rules; do not redistribute raw rows.
