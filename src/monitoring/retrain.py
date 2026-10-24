"""Retraining and champion/challenger promotion (host only; needs mlflow and the local registry).

1. Train a challenger with the champion's recipe on every usable label before the holdout,
   unless the current ``challenger`` was trained on exactly the same rows (same window, labels,
   seed and imbalance method: ``training_fingerprint``); then it is reused, not retrained.
2. Log it to MLflow and register it with alias ``challenger``.
3. Score champion and challenger on the same holdout, paired bootstrap of the PR-AUC difference.
4. Move ``champion`` only if the 95% CI lower bound is above the margin. ``production`` is never
   moved: ``set_alias`` refuses it and the alias is re-read afterwards.

Why host only: the registry is a local SQLite file whose artifact locations are Windows
``file:///C:/...`` paths, which a Linux container cannot open.
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd

from src.data.load import KEY, LABEL
from src.features.entity import SECONDS_PER_DAY
from src.features.history import TIME
from src.models.compare import ComparisonConfig
from src.models.metrics import average_precision, day_block_bootstrap_ap_delta
from src.models.train import fit_on_split, save_bundle
from src.monitoring.config import DriftConfig
from src.monitoring.rules import PromotionDecision, decide_promotion
from src.monitoring.windows import RetrainWindows, check_no_overlap
from src.serving.model import ModelBundle

logger = logging.getLogger(__name__)

CHAMPION = "champion"
CHALLENGER = "challenger"
PRODUCTION = "production"
PROTECTED_ALIASES = frozenset({PRODUCTION})
# Tags on every challenger run; the reuse guard compares them with the planned training rows.
TRAIN_END_TAG = "train_end_dt"
TRAIN_FINGERPRINT_TAG = "train_data_sha256"


class ProtectedAliasError(RuntimeError):
    """Something tried to move an alias the drift job must never touch."""


class Registry(Protocol):
    def alias_version(self, alias: str) -> str | None: ...

    def set_alias(self, alias: str, version: str) -> None: ...


class MlflowRegistry:
    """``fraud-xgboost`` aliases in the MLflow registry at ``uri``."""

    def __init__(self, uri: str) -> None:
        from mlflow.tracking import MlflowClient

        from src.models.tracking import MODEL_NAME

        self.uri = uri
        self.name = MODEL_NAME
        self.client = MlflowClient(tracking_uri=uri, registry_uri=uri)

    def alias_version(self, alias: str) -> str | None:
        from mlflow.exceptions import MlflowException

        try:
            return str(self.client.get_model_version_by_alias(self.name, alias).version)
        except MlflowException:
            return None

    def alias_run_tags(self, alias: str) -> tuple[str, str, dict[str, str]] | None:
        """``(version, run_id, run tags)`` of the aliased version, or None if there is none."""
        from mlflow.exceptions import MlflowException

        try:
            mv = self.client.get_model_version_by_alias(self.name, alias)
        except MlflowException:
            return None
        if not mv.run_id:
            return None
        return str(mv.version), str(mv.run_id), dict(self.client.get_run(mv.run_id).data.tags)

    def set_alias(self, alias: str, version: str) -> None:
        if alias in PROTECTED_ALIASES:
            raise ProtectedAliasError(f"the drift job never moves the {alias!r} alias")
        self.client.set_registered_model_alias(self.name, alias, version)

    def download(self, alias: str, dst: Path) -> Path:
        """The aliased version's model directory, read through this client (a ``models:/``
        URI would resolve against MLflow's global default registry instead)."""
        from src.models.tracking import ARTIFACT_PATH

        version = self.client.get_model_version_by_alias(self.name, alias)
        if not version.run_id:
            raise RuntimeError(f"{self.name}@{alias} has no run to download from")
        path = self.client.download_artifacts(version.run_id, ARTIFACT_PATH, str(dst))
        return Path(path)


def apply_promotion(
    decision: PromotionDecision, registry: Registry, challenger_version: str
) -> dict[str, str | None]:
    """Move ``champion`` if the decision says so; check ``production`` did not move."""
    before = {a: registry.alias_version(a) for a in (CHAMPION, PRODUCTION)}
    if decision.promote:
        registry.set_alias(CHAMPION, challenger_version)
    after = {a: registry.alias_version(a) for a in (CHAMPION, PRODUCTION)}
    if after[PRODUCTION] != before[PRODUCTION]:
        raise ProtectedAliasError(
            f"production alias changed {before[PRODUCTION]} -> {after[PRODUCTION]}"
        )
    return {
        "champion_before": before[CHAMPION],
        "champion_after": after[CHAMPION],
        "production": after[PRODUCTION],
    }


def bootstrap_delta(
    y: np.ndarray[Any, Any],
    champion: np.ndarray[Any, Any],
    challenger: np.ndarray[Any, Any],
    dt: np.ndarray[Any, Any],
    cfg: DriftConfig,
) -> dict[str, float | int]:
    """Paired bootstrap of PR-AUC(challenger) - PR-AUC(champion)."""
    p = cfg.promotion
    unit = dt // SECONDS_PER_DAY if p.unit == "day" else np.arange(len(y))
    return day_block_bootstrap_ap_delta(
        y, champion, challenger, unit, resamples=p.resamples, seed=p.seed
    )


def training_fingerprint(
    train: pd.DataFrame, window_start: int, window_end: int, cfg: DriftConfig
) -> str:
    """Identity of a challenger's training data: sorted TransactionIDs with their labels, the
    window bounds, seed and imbalance method. Equal fingerprints -> the fit would be the same."""
    rows = train[[KEY, LABEL]].sort_values(KEY)
    h = hashlib.sha256()
    h.update(f"{window_start}|{window_end}|{cfg.retrain.seed}|{cfg.retrain.imbalance}|".encode())
    h.update(rows[KEY].to_numpy(dtype=np.int64).tobytes())
    h.update(rows[LABEL].to_numpy(dtype=np.int8).tobytes())
    return h.hexdigest()


def reusable_challenger(
    registry: MlflowRegistry, train_end: int, fingerprint: str
) -> tuple[str, str] | None:
    """``(version, run_id)`` of the current challenger if it was trained on the same rows."""
    current = registry.alias_run_tags(CHALLENGER)
    if current is None:
        return None
    version, run_id, tags = current
    if tags.get(TRAIN_END_TAG) == str(train_end) and tags.get(TRAIN_FINGERPRINT_TAG) == fingerprint:
        return version, run_id
    return None


@dataclass(frozen=True)
class RetrainResult:
    run_id: str
    challenger_version: str
    decision: PromotionDecision
    aliases: dict[str, str | None]
    holdout_rows: int
    holdout_positives: int
    bootstrap: dict[str, float | int]
    reused: bool = False

    def summary(self) -> dict[str, Any]:
        return {
            **self.decision.to_dict(),
            "reused_challenger": self.reused,
            "holdout_rows": self.holdout_rows,
            "holdout_positives": self.holdout_positives,
            "bootstrap": self.bootstrap,
            "aliases": self.aliases,
        }


def retrain_and_compare(
    labelled: pd.DataFrame,
    windows: RetrainWindows,
    champion: ModelBundle,
    cfg: DriftConfig,
    registry: MlflowRegistry,
    *,
    tags: dict[str, str],
) -> RetrainResult:
    """``labelled``: model-frame rows already restricted to usable labels (``usable_labels``)."""
    from src.models.tracking import log_training_run, register_version

    check_no_overlap(windows)
    train = labelled.loc[windows.challenger_train.mask(labelled[TIME])]
    holdout = labelled.loc[windows.holdout.mask(labelled[TIME])]
    if train.empty or holdout.empty or holdout[LABEL].sum() == 0:
        raise RuntimeError("retraining needs training rows and a holdout with at least one fraud")
    for w in windows.all():
        logger.info("window %s", w.describe())

    train_end = windows.challenger_train.end
    fingerprint = training_fingerprint(train, windows.challenger_train.start, train_end, cfg)
    reuse = reusable_challenger(registry, train_end, fingerprint)
    if reuse is not None:
        version, run_id = reuse
        with tempfile.TemporaryDirectory() as tmp:
            challenger = ModelBundle.load(registry.download(CHALLENGER, Path(tmp)))
        logger.info("reused challenger v%s (run %s): same training rows", version, run_id)
    else:
        comparison = ComparisonConfig(seed=cfg.retrain.seed, imbalance=cfg.retrain.imbalance)
        split = {"train_end": windows.holdout.start, "valid_start": windows.holdout.start}
        run = fit_on_split(train, holdout, comparison, split)
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = save_bundle(run.trained, run.metadata, Path(tmp) / "challenger")
            run_id = log_training_run(
                run,
                bundle_dir,
                imbalance_method=cfg.retrain.imbalance,
                uri=registry.uri,  # the same backend as the registry, never the global default
                extra_tags={
                    **tags,
                    "role": CHALLENGER,
                    TRAIN_END_TAG: str(train_end),
                    TRAIN_FINGERPRINT_TAG: fingerprint,
                },
                run_name=f"challenger-{windows.holdout.start}",
            )
            challenger = ModelBundle.load(bundle_dir)
        version = register_version(registry.client, run_id, CHALLENGER)

    y = holdout[LABEL].to_numpy(dtype=np.int64)
    champ_scores = champion.predict(holdout)
    chall_scores = challenger.predict(holdout)
    boot = bootstrap_delta(y, champ_scores, chall_scores, holdout[TIME].to_numpy(), cfg)
    decision = decide_promotion(
        challenger_pr_auc=average_precision(y, chall_scores),
        champion_pr_auc=average_precision(y, champ_scores),
        bootstrap=boot,
        margin=cfg.promotion.margin,
    )
    aliases = apply_promotion(decision, registry, version)
    logger.info(
        "promotion %s: challenger %.4f champion %.4f delta %.4f CI95 [%.4f, %.4f] (%s)",
        "YES" if decision.promote else "NO",
        decision.challenger_pr_auc,
        decision.champion_pr_auc,
        decision.delta_mean,
        decision.ci95_low,
        decision.ci95_high,
        decision.reason,
    )
    return RetrainResult(
        run_id=run_id,
        challenger_version=version,
        decision=decision,
        aliases=aliases,
        holdout_rows=len(holdout),
        holdout_positives=int(y.sum()),
        bootstrap=boot,
        reused=reuse is not None,
    )
