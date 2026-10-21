"""Load and validate the IEEE-CIS Fraud Detection dataset.

Expected layout (files are not committed; see data/README.md):

    data/raw/ieee-cis/{train,test}_transaction.csv
    data/raw/ieee-cis/{train,test}_identity.csv

Transactions are left-joined to identity on ``TransactionID``. Only about a quarter of
transactions have identity rows, so missing device fields after the merge are expected.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pandas as pd

logger = logging.getLogger(__name__)

Split = Literal["train", "test"]

KEY = "TransactionID"
LABEL = "isFraud"
HAS_IDENTITY = "has_identity"

DEFAULT_RAW_DIR = Path(
    os.environ.get(
        "FRAUD_RAW_DATA_DIR",
        Path(__file__).resolve().parents[2] / "data" / "raw" / "ieee-cis",
    )
)

# Columns the planned features depend on (time, amount, user proxy, location, email).
TRANSACTION_NUMERIC = (
    KEY,
    "TransactionDT",
    "TransactionAmt",
    "card1",
    "card2",
    "card3",
    "card5",
    "addr1",
    "addr2",
    "dist1",
    "D1",
)
TRANSACTION_CATEGORICAL = (
    "ProductCD",
    "card4",
    "card6",
    "P_emaildomain",
    "R_emaildomain",
)
IDENTITY_NUMERIC = (KEY,)
IDENTITY_CATEGORICAL = ("DeviceType", "DeviceInfo")

CSV_CHUNK_ROWS = 100_000

# Keys and the label can never be missing; other required columns may contain NaN.
NON_NULL = (KEY, "TransactionDT", "TransactionAmt")


class DataFileNotFoundError(FileNotFoundError):
    """A required dataset file is absent."""


class SchemaError(ValueError):
    """A dataset file does not match the expected schema."""


class DuplicateKeyError(SchemaError):
    """The join key is not unique within a file."""


@dataclass(frozen=True)
class IEEECISPaths:
    transaction: Path
    identity: Path

    @classmethod
    def from_dir(
        cls, raw_dir: Path | str = DEFAULT_RAW_DIR, split: Split = "train"
    ) -> IEEECISPaths:
        raw_dir = Path(raw_dir)
        return cls(
            transaction=raw_dir / f"{split}_transaction.csv",
            identity=raw_dir / f"{split}_identity.csv",
        )


def _require_file(path: Path) -> None:
    if not path.is_file():
        raise DataFileNotFoundError(
            f"Dataset file not found: {path}. Download IEEE-CIS from "
            "https://www.kaggle.com/c/ieee-fraud-detection/data and place the CSVs in "
            f"{path.parent} (see data/README.md)."
        )


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    # The Kaggle test identity file uses "id-01" while train uses "id_01".
    return df.rename(columns=lambda c: c.strip().replace("-", "_"))


def _check_columns(df: pd.DataFrame, required: Iterable[str], source: Path) -> None:
    missing = sorted(set(required) - set(df.columns))
    if missing:
        raise SchemaError(f"{source.name} is missing required columns: {missing}")


def _check_numeric(df: pd.DataFrame, columns: Iterable[str], source: Path) -> None:
    bad = [c for c in columns if not pd.api.types.is_numeric_dtype(df[c])]
    if bad:
        raise SchemaError(f"{source.name} has non-numeric values in numeric columns: {bad}")


def _check_non_null(df: pd.DataFrame, columns: Iterable[str], source: Path) -> None:
    nulls = {c: int(df[c].isna().sum()) for c in columns if c in df.columns}
    nulls = {c: n for c, n in nulls.items() if n}
    if nulls:
        raise SchemaError(f"{source.name} has nulls in non-nullable columns: {nulls}")


def _check_unique_key(df: pd.DataFrame, source: Path) -> None:
    dupes = df[KEY].duplicated(keep=False)
    if dupes.any():
        sample = df.loc[dupes, KEY].drop_duplicates().head(5).tolist()
        raise DuplicateKeyError(
            f"{source.name} has {int(dupes.sum())} rows with duplicate {KEY}; examples: {sample}"
        )


def _optimize_dtypes(df: pd.DataFrame, categorical: Iterable[str]) -> pd.DataFrame:
    """Downcast floats and convert low-cardinality strings to category (~2x less memory)."""
    for col in df.select_dtypes(include="float64").columns:
        df[col] = df[col].astype("float32")
    for col in categorical:
        if col in df.columns:
            df[col] = df[col].astype("category")
    df[KEY] = df[KEY].astype("int64")
    return df


def _read_csv(
    path: Path,
    usecols: Iterable[str] | None,
    required: Iterable[str],
    chunksize: int | None = None,
) -> pd.DataFrame:
    """Read in chunks, downcasting floats per chunk to bound peak memory on the 683 MB file."""
    _require_file(path)
    wanted = None if usecols is None else {c.replace("-", "_") for c in usecols} | set(required)
    try:
        reader = pd.read_csv(
            path,
            usecols=None if wanted is None else (lambda c: c.strip().replace("-", "_") in wanted),
            low_memory=False,
            chunksize=chunksize or CSV_CHUNK_ROWS,
        )
        chunks = []
        for chunk in reader:
            floats = chunk.select_dtypes(include="float64").columns
            chunks.append(chunk.astype(dict.fromkeys(floats, "float32")))
    except pd.errors.EmptyDataError as exc:
        raise SchemaError(f"{path.name} is empty") from exc
    df = pd.concat(chunks, ignore_index=True) if len(chunks) > 1 else chunks[0]
    return _normalize_columns(df)


def load_transactions(
    path: Path | str,
    *,
    require_label: bool = True,
    usecols: Iterable[str] | None = None,
) -> pd.DataFrame:
    """Load and validate a ``*_transaction.csv`` file.

    ``usecols`` selects extra columns; required columns are always loaded.
    """
    path = Path(path)
    numeric = TRANSACTION_NUMERIC + ((LABEL,) if require_label else ())
    required = numeric + TRANSACTION_CATEGORICAL
    df = _read_csv(path, usecols, required)

    _check_columns(df, required, path)
    _check_numeric(df, numeric, path)
    _check_non_null(df, NON_NULL + ((LABEL,) if require_label else ()), path)
    _check_unique_key(df, path)

    if (df["TransactionDT"] < 0).any() or (df["TransactionAmt"] < 0).any():
        raise SchemaError(f"{path.name} has negative TransactionDT or TransactionAmt")
    if require_label:
        labels = set(df[LABEL].unique())
        if not labels <= {0, 1}:
            raise SchemaError(f"{path.name} has invalid {LABEL} values: {sorted(labels - {0, 1})}")
        df[LABEL] = df[LABEL].astype("int8")

    df = _optimize_dtypes(df, TRANSACTION_CATEGORICAL)
    logger.info("loaded transactions file=%s rows=%d cols=%d", path.name, len(df), df.shape[1])
    return df


def load_identity(path: Path | str, *, usecols: Iterable[str] | None = None) -> pd.DataFrame:
    """Load and validate a ``*_identity.csv`` file."""
    path = Path(path)
    required = IDENTITY_NUMERIC + IDENTITY_CATEGORICAL
    df = _read_csv(path, usecols, required)

    _check_columns(df, required, path)
    _check_numeric(df, IDENTITY_NUMERIC, path)
    _check_non_null(df, (KEY,), path)
    _check_unique_key(df, path)

    df = _optimize_dtypes(df, IDENTITY_CATEGORICAL)
    logger.info("loaded identity file=%s rows=%d cols=%d", path.name, len(df), df.shape[1])
    return df


def merge_transaction_identity(transactions: pd.DataFrame, identity: pd.DataFrame) -> pd.DataFrame:
    """Left-join identity onto transactions, preserving every transaction and its order.

    Adds a boolean ``has_identity`` column. Identity rows with no matching transaction are
    dropped and logged.
    """
    overlap = (set(transactions.columns) & set(identity.columns)) - {KEY}
    if overlap:
        raise SchemaError(f"transaction and identity share non-key columns: {sorted(overlap)}")

    merged = transactions.merge(
        identity, on=KEY, how="left", validate="one_to_one", indicator=HAS_IDENTITY, sort=False
    )
    merged[HAS_IDENTITY] = merged[HAS_IDENTITY].eq("both")

    orphans = len(identity) - int(merged[HAS_IDENTITY].sum())
    if orphans:
        logger.warning("dropped identity rows without a matching transaction count=%d", orphans)
    logger.info(
        "merged rows=%d with_identity=%d (%.1f%%)",
        len(merged),
        int(merged[HAS_IDENTITY].sum()),
        100 * merged[HAS_IDENTITY].mean() if len(merged) else 0.0,
    )
    return merged


def load_ieee_cis(
    raw_dir: Path | str = DEFAULT_RAW_DIR,
    split: Split = "train",
    *,
    usecols: Iterable[str] | None = None,
) -> pd.DataFrame:
    """Load, validate and merge the transaction and identity files for one split."""
    paths = IEEECISPaths.from_dir(raw_dir, split)
    # Fail on any missing file before spending time parsing the large transaction CSV.
    _require_file(paths.transaction)
    _require_file(paths.identity)
    transactions = load_transactions(
        paths.transaction, require_label=split == "train", usecols=usecols
    )
    identity = load_identity(paths.identity, usecols=usecols)
    return merge_transaction_identity(transactions, identity)


def main() -> None:
    """Print a quick sanity summary: ``python -m src.data.load``."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    df = load_ieee_cis()
    print(
        f"rows={len(df)} cols={df.shape[1]} memory_mb={df.memory_usage(deep=True).sum() / 1e6:.0f}"
    )
    print(f"fraud_rate={df[LABEL].mean():.4%} has_identity={df[HAS_IDENTITY].mean():.2%}")


if __name__ == "__main__":
    main()
