"""Score one transaction event: dedupe -> online features -> model -> persist."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from src.serving.model import ModelBundle
from src.serving.online_features import (
    entity_keys,
    history_feature_values,
    online_model_row,
)
from src.serving.schema import ScoreResponse, TransactionEvent
from src.serving.store import ScoredRecord, TransactionStore

logger = logging.getLogger(__name__)


@dataclass
class Scorer:
    bundle: ModelBundle
    store: TransactionStore

    def _duplicate(self, record: ScoredRecord, event: dict, started: float) -> ScoreResponse:
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
        history = [] if card_id is None else self.store.card_history(card_id, event.TransactionDT)
        row = online_model_row(ev, history, self.bundle.history_config)
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

        latency_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "scored id=%d score=%.4f alert=%s history_rows=%d latency_ms=%.1f",
            record.transaction_id,
            score,
            record.is_alert,
            len(history),
            latency_ms,
        )
        return ScoreResponse(
            transaction_id=record.transaction_id,
            score=score,
            is_alert=record.is_alert,
            threshold=record.threshold,
            model_version=record.model_version,
            duplicate=False,
            history_rows=len(history),
            latency_ms=latency_ms,
            history_features=history_feature_values(row, self.bundle.history_config),
        )
