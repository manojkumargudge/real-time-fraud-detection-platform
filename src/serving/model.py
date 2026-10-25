"""Load and validate the saved baseline model bundle (``artifacts/baseline``)."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xgboost as xgb

from src.array_types import FloatArray
from src.features.history import HistoryConfig
from src.features.preprocess import UNSEEN, CategoricalEncoder
from src.models.compare import feature_columns
from src.models.train import METADATA_FILE, MODEL_FILE, SCHEMA_VERSION

logger = logging.getLogger(__name__)


class ModelBundleError(RuntimeError):
    """The model bundle is missing, corrupt, or incompatible with this code version."""


@dataclass(frozen=True)
class ModelBundle:
    booster: xgb.Booster
    encoder: CategoricalEncoder
    history_config: HistoryConfig
    feature_columns: list[str]
    threshold: float
    model_version: str
    metadata: dict[str, Any]

    @classmethod
    def load(cls, directory: Path | str) -> ModelBundle:
        directory = Path(directory)
        model_path, meta_path = directory / MODEL_FILE, directory / METADATA_FILE
        for path in (model_path, meta_path):
            if not path.is_file():
                raise ModelBundleError(
                    f"missing {path}; run `python -m src.models.train` to create the bundle"
                )
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("schema_version") != SCHEMA_VERSION:
            raise ModelBundleError(
                f"unsupported bundle schema_version {meta.get('schema_version')}"
            )
        if meta.get("variant") != "baseline":
            raise ModelBundleError("only the baseline (card_id/uid) variant may be served")
        if hashlib.sha256(model_path.read_bytes()).hexdigest() != meta.get("model_sha256"):
            raise ModelBundleError("model.json does not match model_sha256 in metadata")

        config = HistoryConfig(
            windows=tuple((k, int(v)) for k, v in meta["history_windows"].items()),
            change_columns=tuple(meta["change_columns"]),
        )
        expected = feature_columns(config)
        if meta["feature_columns"] != expected:
            raise ModelBundleError(
                "bundle feature schema differs from the current feature code; retrain the model"
            )
        threshold = float(meta["threshold"])
        # A NaN threshold would make `score >= threshold` always False: silently no alerts.
        if not (np.isfinite(threshold) and 0.0 <= threshold <= 1.0):
            raise ModelBundleError(f"threshold must be a probability in [0, 1], got {threshold}")
        booster = xgb.Booster()
        booster.load_model(model_path)
        # Serving predicts one row at a time: with the default (all cores) every call starts
        # an OpenMP team, about 10 ms per row instead of 0.5 ms, and 3 consumers oversubscribe
        # the CPU. Thread count does not change the scores (each row is one tree walk).
        booster.set_param({"nthread": 1})
        if booster.feature_names != expected:
            raise ModelBundleError("booster feature names do not match the metadata schema")
        bundle = cls(
            booster=booster,
            encoder=CategoricalEncoder.from_dict(meta["encoder"]),
            history_config=config,
            feature_columns=expected,
            threshold=threshold,
            model_version=str(meta["model_version"]),
            metadata=meta,
        )
        logger.info("loaded model version=%s features=%d", bundle.model_version, len(expected))
        return bundle

    def predict(self, frame: pd.DataFrame) -> FloatArray:
        """Fraud probabilities for model-frame rows (see ``compare.build_model_frame``)."""
        if len(frame) == 1:
            x = self._encode_row(frame)
        else:
            x = self.encoder.transform(frame[self.feature_columns]).to_numpy(dtype=np.float32)
        # inplace_predict skips DMatrix construction, and a NumPy array (columns in
        # feature_columns order = booster.feature_names, checked at load) skips the per-call
        # DataFrame conversion (same scores, lower per-event overhead).
        scores: FloatArray = self.booster.inplace_predict(x, missing=np.nan)
        return scores

    def _encode_row(self, frame: pd.DataFrame) -> FloatArray:
        """One row of ``encoder.transform(...).astype("float32")`` without pandas per-column
        work (the serving path scores one event at a time; equality is tested)."""
        vocab = self.encoder.vocab_
        if vocab is None:
            raise ModelBundleError("bundle encoder is not fitted")
        values = frame[self.feature_columns].iloc[0]
        out = np.empty((1, len(self.feature_columns)), dtype=np.float32)
        for i, col in enumerate(self.feature_columns):
            v = values[col]
            if col in vocab:  # transform: present -> str -> code (unseen -1), missing -> NaN
                out[0, i] = np.nan if pd.isna(v) else vocab[col].get(str(v), UNSEEN)
            else:
                out[0, i] = np.nan if v is None else v
        return out
