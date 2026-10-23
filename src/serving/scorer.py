"""Score one transaction event: dedupe -> online features -> model -> persist.

History features come from the store's event history (default) or, with ``read_from_feast``,
from the Feast entity state. With ``feast`` set, every newly saved event also updates that state
(docs/feature_store.md), whichever path computed its features.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.serving.model import ModelBundle
from src.serving.online_features import (
    entity_keys,
    history_feature_values,
    model_row_from_features,
    online_model_row,
)
from src.serving.schema import ScoreResponse, TransactionEvent
from src.serving.store import ScoredRecord, TransactionStore

if TYPE_CHECKING:
    from src.features.feast_store import FeastFeatures

logger = logging.getLogger(__name__)


@dataclass
class Scorer:
    bundle: ModelBundle
    store: TransactionStore
    feast: FeastFeatures | None = None
    read_from_feast: bool = False

    def __post_init__(self) -> None:
        if self.read_from_feast and self.feast is None:
            raise ValueError("read_from_feast needs a FeastFeatures instance")

    @property
    def feature_source(self) -> str:
        return "feast" if self.read_from_feast else "store"

    def _duplicate(
        self, record: ScoredRecord, event: dict[str, Any], started: float
    ) -> ScoreResponse:
        if record.event != event:
            logger.warning(
                "duplicate transaction_id with different payload id=%d; returning stored score",
                record.transaction_id,
            )
        return ScoreResponse(
            transaction_id=record.transaction_id,
            score=record.score,
            is_alert=record.is_alert,
            threshold=record.threshold,
            model_version=record.model_version,
            duplicate=True,
            history_rows=0,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    def score_event(self, event: TransactionEvent) -> ScoreResponse:
        started = time.perf_counter()
        ev = event.to_record()
        existing = self.store.get(event.TransactionID)
        if existing is not None:
            return self._duplicate(existing, ev, started)

        card_id, uid = entity_keys(ev)
        config = self.bundle.history_config
        online: dict[str, Any] | None = None
        if self.read_from_feast and self.feast is not None:
            feats, online = self.feast.read(ev, card_id, uid)
            row = model_row_from_features(ev, feats, config)
            prior = feats["uid_prior_txn_count"]
            history_rows = 0 if prior != prior else int(prior)  # NaN (missing key) -> 0
        else:
            history = (
                [] if card_id is None else self.store.card_history(card_id, event.TransactionDT)
            )
            row = online_model_row(ev, history, config)
            history_rows = len(history)
        score = float(self.bundle.predict(row)[0])
        record = ScoredRecord(
            transaction_id=event.TransactionID,
            transaction_dt=event.TransactionDT,
            card_id=card_id,
            uid=uid,
            event=ev,
            score=score,
            threshold=self.bundle.threshold,
            is_alert=score >= self.bundle.threshold,
            model_version=self.bundle.model_version,
        )
        if not self.store.save(record):  # lost a race with a concurrent duplicate
            stored = self.store.get(event.TransactionID)
            if stored is None:
                raise RuntimeError(f"save reported a duplicate but id {event.TransactionID} absent")
            return self._duplicate(stored, ev, started)
        if self.feast is not None:
            self._update_feast(ev, card_id, uid, online)

        latency_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "scored id=%d score=%.4f alert=%s history_rows=%d features=%s latency_ms=%.1f",
            record.transaction_id,
            score,
            record.is_alert,
            history_rows,
            self.feature_source,
            latency_ms,
        )
        return ScoreResponse(
            transaction_id=record.transaction_id,
            score=score,
            is_alert=record.is_alert,
            threshold=record.threshold,
            model_version=record.model_version,
            duplicate=False,
            history_rows=history_rows,
            latency_ms=latency_ms,
            history_features=history_feature_values(row, config),
        )

    def _update_feast(
        self,
        ev: dict[str, Any],
        card_id: str | None,
        uid: str | None,
        online: dict[str, Any] | None,
    ) -> None:
        """Push the new state. The event is already saved, so a failure here must not fail the
        request: the state goes stale until the next ``materialize`` (logged as an error)."""
        assert self.feast is not None
        try:
            if online is None:
                _, online = self.feast.read(ev, card_id, uid)
            self.feast.push(ev, card_id, uid, online)
        except Exception:
            logger.exception(
                "feast state update failed id=%s; rerun materialize", ev.get("TransactionID")
            )
