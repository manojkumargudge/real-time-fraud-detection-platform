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


REGISTRY_FILE = "registry.json"  # written by `python -m src.models.link_registry` (host)
REGISTRY_KEYS = ("name", "version", "alias", "run_id", "model_sha256", "linked_at")


@dataclass(frozen=True)
class RegistryLink:
    """The MLflow registry version the served ``model.json`` was verified against."""

    name: str
    version: str
    alias: str
    run_id: str
    model_sha256: str
    linked_at: str


def load_registry_link(directory: Path, model_sha256: str) -> RegistryLink | None:
    """``registry.json`` if present (None if absent). A link whose ``model_sha256`` differs from
    the loaded model, or that is malformed, stops the load: a wrong link is worse than none."""
    path = directory / REGISTRY_FILE
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        link = RegistryLink(**{k: str(raw[k]) for k in REGISTRY_KEYS})
    except (ValueError, KeyError, TypeError) as exc:
        raise ModelBundleError(f"malformed {path}: {exc}") from exc
    if link.model_sha256 != model_sha256:
        raise ModelBundleError(
            f"{path} links {link.name} v{link.version} with model_sha256 "
            f"{link.model_sha256[:12]}..., but the loaded model.json is {model_sha256[:12]}...; "
            "re-run `python -m src.models.link_registry` or remove the file"
        )
    return link


@dataclass(frozen=True)
class ModelBundle:
    booster: xgb.Booster
    encoder: CategoricalEncoder
    history_config: HistoryConfig
    feature_columns: list[str]
    threshold: float
    model_version: str
    metadata: dict[str, Any]
    registry: RegistryLink | None = None  # None = not linked (no registry.json)

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
        model_sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest()
        if model_sha256 != meta.get("model_sha256"):
            raise ModelBundleError("model.json does not match model_sha256 in metadata")
        registry = load_registry_link(directory, model_sha256)

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
            registry=registry,
        )
        logger.info(
            "loaded model version=%s features=%d registry=%s",
            bundle.model_version,
            len(expected),
            f"{registry.name} v{registry.version} ({registry.alias})" if registry else "not linked",
        )
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
