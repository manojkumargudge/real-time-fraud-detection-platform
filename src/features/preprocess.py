"""Preprocessing whose state is fitted on the training split only.

``CategoricalEncoder`` maps string categories to integer codes using a vocabulary learned from
training rows. Encoding (float32, so XGBoost can use NaN natively):

* known category  -> 0..k-1 (vocabulary sorted, so codes are deterministic)
* unseen or rare  -> -1
* missing         -> NaN

Whole-dataset frequency/target encodings are deliberately absent: fitted on the full training
window they leak later activity into earlier rows. Causal counts live in ``history.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.data.load import LABEL

logger = logging.getLogger(__name__)

DEFAULT_CATEGORICAL = (
    "ProductCD",
    "card4",
    "card6",
    "P_emaildomain",
    "R_emaildomain",
    "DeviceType",
    "DeviceInfo",
)
UNSEEN = -1


class NotFittedError(RuntimeError):
    """transform() was called before fit()."""


@dataclass
class CategoricalEncoder:
    columns: tuple[str, ...] = DEFAULT_CATEGORICAL
    min_count: int = 1
    vocab_: dict[str, dict[str, int]] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if LABEL in self.columns:
            raise ValueError(f"{LABEL} must not be encoded as a feature")
        if self.min_count < 1:
            raise ValueError("min_count must be >= 1")

    def fit(self, train: pd.DataFrame) -> CategoricalEncoder:
        self._check_columns(train)
        vocab: dict[str, dict[str, int]] = {}
        for col in self.columns:
            counts = train[col].dropna().astype(str).value_counts()
            kept = sorted(counts[counts >= self.min_count].index)
            vocab[col] = {v: i for i, v in enumerate(kept)}
        self.vocab_ = vocab
        logger.info(
            "fitted categorical encoder rows=%d vocab_sizes=%s",
            len(train),
            {c: len(v) for c, v in vocab.items()},
        )
        return self

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return a copy of ``df`` with ``columns`` replaced by codes."""
        if self.vocab_ is None:
            raise NotFittedError("CategoricalEncoder.fit must be called before transform")
        self._check_columns(df)
        out = df.copy()
        for col in self.columns:
            s = df[col]
            present = s.notna().to_numpy()
            codes = np.full(len(s), np.nan, dtype=np.float32)
            mapped = s[present].astype(str).map(self.vocab_[col]).fillna(UNSEEN)
            codes[present] = mapped.to_numpy(dtype=np.float32)
            out[col] = codes
        return out

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable state, so serving can apply the identical mapping."""
        if self.vocab_ is None:
            raise NotFittedError("nothing to serialise before fit")
        return {"columns": list(self.columns), "min_count": self.min_count, "vocab": self.vocab_}

    @classmethod
    def from_dict(cls, state: dict[str, Any]) -> CategoricalEncoder:
        enc = cls(columns=tuple(state["columns"]), min_count=int(state["min_count"]))
        enc.vocab_ = {c: dict(v) for c, v in state["vocab"].items()}
        return enc

    def _check_columns(self, df: pd.DataFrame) -> None:
        missing = sorted(set(self.columns) - set(df.columns))
        if missing:
            raise ValueError(f"frame is missing categorical columns: {missing}")
