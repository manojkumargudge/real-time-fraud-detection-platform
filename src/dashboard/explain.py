"""Why flagged: per-feature contributions of the served XGBoost model for one transaction.

Read-only; nothing here writes to a database or changes a score.

* **Feature vector.** The scoring row stores the raw event but not the 12 history features
  (they came from the Feast state at scoring time, which has moved on since). The vector is
  therefore rebuilt with the point-in-time batch code the model was trained with
  (``build_model_frame(df, "baseline")``); the Feast serving path equals it (4,000 replayed
  scores bit-identical, docs/serving.md). ``python -m src.dashboard.explain build`` writes it
  for the validation window to ``FEATURES_FILE`` (generated, gitignored), which the dashboard
  mounts read-only.
* **Contributions.** ``Booster.predict(..., pred_contribs=True)`` of the served bundle
  (XGBoost's built-in TreeSHAP, log-odds; the last column is the bias). No shap package.
* **Parity check.** For each case: sum of contributions + bias must equal the raw margin, and
  sigmoid(margin) must equal the stored score within ``SCORE_TOLERANCE``. If not, the panel shows
  a warning instead of bars: the vector is then not the one that was scored.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

from src.array_types import FloatArray
from src.serving.model import ModelBundle

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
KEY = "TransactionID"
FEATURES_FILE = Path(
    os.environ.get("EXPLAIN_FEATURES_FILE", ROOT / "artifacts" / "explain" / "features.csv.gz")
)
MODEL_DIR = Path(os.environ.get("EXPLAIN_MODEL_DIR", ROOT / "artifacts" / "baseline"))
SCORE_TOLERANCE = 1e-6  # |sigmoid(margin) - stored score|
MARGIN_TOLERANCE = 1e-5  # |sum(contributions) + bias - margin|: float32 sums (max 5.4e-6 seen)
TOP_UP = 5
TOP_DOWN = 3
CAPTION = "Contributions explain the model's score, not the cause of fraud."

ANONYMISED = ("card1", "card2", "card3", "card5", "addr1", "addr2", "dist1", "ProductCD")
LABELS = {
    "TransactionAmt": "Transaction amount (USD)",
    "has_identity": "Has an identity record",
    "card4": "Card network (card4)",
    "card6": "Card type (card6)",
    "P_emaildomain": "Purchaser email domain",
    "R_emaildomain": "Recipient email domain",
    "DeviceType": "Device type",
    "DeviceInfo": "Device model / OS",
    "uid_prior_txn_count": "Customer's earlier transactions",
    "uid_txn_count_1h": "Customer's transactions in the last hour",
    "uid_txn_count_24h": "Customer's transactions in the last 24 hours",
    "uid_txn_count_7d": "Customer's transactions in the last 7 days",
    "uid_txn_count_30d": "Customer's transactions in the last 30 days",
    "uid_seconds_since_prev_txn": "Seconds since the customer's previous transaction",
    "uid_prior_amt_mean": "Customer's average earlier amount",
    "uid_amt_to_prior_mean": "Amount vs the customer's average (ratio)",
    "card_deviceinfo_changed": "Device changed on this card",
    "card_devicetype_changed": "Device type changed on this card",
    "card_addr1_changed": "Billing region changed on this card",
    "card_addr2_changed": "Billing country changed on this card",
}


def feature_label(column: str) -> str:
    """Plain-English name; anonymised IEEE-CIS columns keep their code name."""
    if column in ANONYMISED:
        return f"{column} (anonymised)"
    return LABELS.get(column, column)


@dataclass(frozen=True)
class Contribution:
    feature: str
    label: str
    value: object
    contribution: float  # log-odds


@dataclass(frozen=True)
class Explanation:
    transaction_id: int
    raising: list[Contribution]
    lowering: list[Contribution]
    bias: float
    margin: float
    contribution_sum: float  # sum over features + bias
    model_score: float  # sigmoid(margin)
    stored_score: float

    @property
    def margin_diff(self) -> float:
        return abs(self.contribution_sum - self.margin)

    @property
    def score_diff(self) -> float:
        return abs(self.model_score - self.stored_score)

    @property
    def parity_ok(self) -> bool:
        return self.margin_diff <= MARGIN_TOLERANCE and self.score_diff <= SCORE_TOLERANCE


def contributions(bundle: ModelBundle, frame: pd.DataFrame) -> tuple[FloatArray, FloatArray]:
    """``(contribs, margin)``: per-row ``(n, k + 1)`` log-odds contributions (last column =
    bias) and the raw margin, from the bundle's booster on its own encoding."""
    x = bundle.encoder.transform(frame[bundle.feature_columns]).to_numpy(dtype=np.float32)
    matrix = xgb.DMatrix(x, missing=np.nan, feature_names=bundle.feature_columns)
    contribs = np.asarray(bundle.booster.predict(matrix, pred_contribs=True), dtype=np.float64)
    margin = np.asarray(bundle.booster.predict(matrix, output_margin=True), dtype=np.float64)
    return contribs, margin


def explain_row(
    bundle: ModelBundle,
    row: pd.DataFrame,
    stored_score: float,
    transaction_id: int,
    top_up: int = TOP_UP,
    top_down: int = TOP_DOWN,
) -> Explanation:
    """Top features raising / lowering one transaction's score, with the parity check."""
    if len(row) != 1:
        raise ValueError(f"explain_row needs exactly one row, got {len(row)}")
    contribs, margin = contributions(bundle, row)
    values, bias = contribs[0, :-1], float(contribs[0, -1])
    items = [
        Contribution(col, feature_label(col), row.iloc[0][col], float(v))
        for col, v in zip(bundle.feature_columns, values, strict=True)
    ]
    up = sorted((c for c in items if c.contribution > 0), key=lambda c: -c.contribution)
    down = sorted((c for c in items if c.contribution < 0), key=lambda c: c.contribution)
    m = float(margin[0])
    return Explanation(
        transaction_id=transaction_id,
        raising=up[:top_up],
        lowering=down[:top_down],
        bias=bias,
        margin=m,
        contribution_sum=float(values.sum()) + bias,
        model_score=1.0 / (1.0 + math.exp(-m)),
        stored_score=float(stored_score),
    )


def read_features(path: Path, columns: list[str]) -> pd.DataFrame:
    """The feature file, indexed by TransactionID (categoricals as text, missing as NaN)."""
    categorical = {"ProductCD", "card4", "card6", "P_emaildomain", "R_emaildomain"}
    categorical |= {"DeviceType", "DeviceInfo"}
    dtypes = {c: (object if c in categorical else np.float64) for c in columns}
    frame = pd.read_csv(
        path, dtype={KEY: np.int64, **dtypes}, keep_default_na=False, na_values=[""]
    )
    return frame.set_index(KEY)[columns]


class Explainer:
    """The served bundle plus the feature file; ``explain`` returns None for an unknown id."""

    def __init__(self, bundle: ModelBundle, features: pd.DataFrame) -> None:
        self.bundle = bundle
        self.features = features

    @classmethod
    def load(cls, model_dir: Path = MODEL_DIR, features_file: Path = FEATURES_FILE) -> Explainer:
        bundle = ModelBundle.load(model_dir)
        return cls(bundle, read_features(features_file, bundle.feature_columns))

    def explain(self, transaction_id: int, stored_score: float) -> Explanation | None:
        if transaction_id not in self.features.index:
            return None
        row = self.features.loc[[transaction_id]]
        return explain_row(self.bundle, row, stored_score, transaction_id)


def build(out: Path = FEATURES_FILE) -> Path:
    """Write the point-in-time feature vectors of the validation window (host, needs raw data)."""
    from src.data.load import DEFAULT_RAW_DIR, load_ieee_cis
    from src.features.history import TIME, HistoryConfig
    from src.features.split import validation_start
    from src.models.compare import build_model_frame, feature_columns

    df = load_ieee_cis(DEFAULT_RAW_DIR, usecols=[])
    config = HistoryConfig()
    frame = build_model_frame(df, "baseline", config)
    keep = (df[TIME] >= validation_start(df[TIME])).to_numpy()
    cols = feature_columns(config)
    out_frame = pd.concat([df.loc[keep, [KEY]], frame.loc[keep, cols]], axis=1)
    write_features(out_frame, out)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    logger.info("wrote %s rows=%d columns=%d sha256=%s", out, len(out_frame), len(cols), digest)
    return out


def write_features(frame: pd.DataFrame, out: Path) -> None:
    """``TransactionID`` + feature columns as (gzipped) CSV; floats round-trip exactly."""
    out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(out, index=False, float_format="%.17g")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="write the validation-window feature file")
    b.add_argument("--out", type=Path, default=FEATURES_FILE)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command == "build":
        build(args.out)


if __name__ == "__main__":
    main()
