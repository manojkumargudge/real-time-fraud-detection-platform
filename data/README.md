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
.venv\Scripts\python -m src.data.load
.venv\Scripts\python -m pytest -m real_data
```

Usage is subject to the Kaggle competition rules; do not redistribute raw rows.
