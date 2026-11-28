"""Score one transaction event: dedupe -> online features -> model -> persist.

History features come from the store's event history (default) or, with ``read_from_feast``,
from the Feast entity state. With ``feast`` set, every newly saved event also updates that state
(docs/feature_store.md), whichever path computed its features.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

import pandas as pd

from src.serving.model import ModelBundle
from src.serving.online_features import (
    entity_keys_from_frame,
    events_to_frame,
    history_feature_values,
    model_row_from_features,
    online_model_row,
)
from src.serving.schema import ScoreResponse, TransactionEvent
from src.serving.store import PostgresStore, ScoredRecord, TransactionStore
from src.timing import stage

if TYPE_CHECKING:
    from src.features.feast_store import FeastFeatures

logger = logging.getLogger(__name__)


class LateEventError(ValueError):
    """The online state has advanced past the input event timestamp."""


class PendingFeastUpdateError(ConnectionError):
    """A committed score has a pending feature update that must be retried."""


class FeastHistoryUnavailableError(ConnectionError):
    """An online entity row is missing despite durable scoring history."""


@dataclass
class Scorer:
    bundle: ModelBundle
    store: TransactionStore
    feast: FeastFeatures | None = None
    read_from_feast: bool = False
    reject_late: bool = False
    feast_generation: str | None = None

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
            logger.warning("duplicate transaction_id with different payload")
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
        with stage("dedupe_get"):
            existing = self.store.get(event.TransactionID)
        if existing is not None:
            if (
                existing.card_id is not None
                and self.feast
                and isinstance(self.store, PostgresStore)
            ):
                with self.store.serialize_card(existing.card_id):
                    self._check_generation(self.store)
                    self._recover_feast(self.store, existing.card_id)
            return self._duplicate(existing, ev, started)

        with stage("entity_keys"):
            frame = events_to_frame([ev])  # once per event: keys and the model row
            card_id, uid = entity_keys_from_frame(frame)
        if card_id is not None and self.feast and isinstance(self.store, PostgresStore):
            with self.store.serialize_card(card_id):
                self._check_generation(self.store)
                self._recover_feast(self.store, card_id)
                existing = self.store.get(event.TransactionID)
                if existing is not None:
                    return self._duplicate(existing, ev, started)
                return self._score_new(event, ev, frame, card_id, uid, started, self.store)
        return self._score_new(event, ev, frame, card_id, uid, started)

    def _score_new(
        self,
        event: TransactionEvent,
        ev: dict[str, Any],
        frame: pd.DataFrame,
        card_id: str | None,
        uid: str | None,
        started: float,
        durable: PostgresStore | None = None,
    ) -> ScoreResponse:
        config = self.bundle.history_config
        online: dict[str, Any] | None = None
        if self.read_from_feast and self.feast is not None:
            if (
                self.feast_generation is not None
                and durable is not None
                and card_id is not None
                and uid is not None
            ):
                try:
                    feats, online = self.feast.read(
                        ev,
                        card_id,
                        uid,
                        validate=partial(self._verify_online, durable, card_id, uid),
                    )
                except FeastHistoryUnavailableError:
                    raise
                except Exception:
                    raise FeastHistoryUnavailableError(
                        "Cannot read verified Redis history"
                    ) from None
            else:
                feats, online = self.feast.read(ev, card_id, uid)
            if durable is not None and card_id is not None and uid is not None:
                card_missing = online.get("card_last_ts") is None
                uid_missing = online.get("uid_last_ts") is None
                if (card_missing or uid_missing) and durable.has_scored_entity(
                    card_id, uid, any_uid=card_missing
                ):
                    raise FeastHistoryUnavailableError("Restore missing online feature history")
            if self.reject_late and any(
                online.get(field) is not None and online[field] > event.TransactionDT
                for field in ("uid_last_ts", "card_last_ts")
            ):
                # A parallel delivery may have committed while this request waited for the lock.
                existing = self.store.get(event.TransactionID)
                if existing is not None:
                    return self._duplicate(existing, ev, started)
                raise LateEventError("Transaction timestamp predates online feature state")
            with stage("model_frame"):
                row = model_row_from_features(frame, feats, config)
            prior = feats["uid_prior_txn_count"]
            history_rows = 0 if prior != prior else int(prior)  # NaN (missing key) -> 0
        else:
            history = (
                [] if card_id is None else self.store.card_history(card_id, event.TransactionDT)
            )
            row = online_model_row(ev, history, config)
            history_rows = len(history)
        with stage("predict"):
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
        with stage("pg_save"):
            if durable is not None and card_id is not None and uid is not None:
                assert self.feast is not None
                if online is None:
                    _, online = self.feast.read(ev, card_id, uid)
                saved = durable.save(record, feast_online=online)
            else:
                saved = self.store.save(record)
        if not saved:  # lost a race with a concurrent duplicate
            stored = self.store.get(event.TransactionID)
            if stored is None:
                raise RuntimeError("save reported a duplicate but the record is absent")
            return self._duplicate(stored, ev, started)
        if self.feast is not None:
            with stage("feast_push"):
                if durable is not None and card_id is not None and uid is not None:
                    assert online is not None
                    self._push_pending(durable, card_id, uid, ev, online)
                else:
                    self._update_feast(ev, card_id, uid, online)

        latency_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "scored history_rows=%d features=%s latency_ms=%.1f",
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

    def _recover_feast(self, store: PostgresStore, card_id: str) -> None:
        pending = store.pending_feast(card_id)
        if pending is not None:
            self._push_pending(store, card_id, pending["uid"], pending["event"], pending["online"])

    def _check_generation(self, store: PostgresStore) -> None:
        checkpoint = store.feast_checkpoint()
        if self.feast_generation != (None if checkpoint is None else checkpoint[0]):
            raise FeastHistoryUnavailableError(
                "Restart scoring against the promoted feature generation"
            )

    def _verify_online(
        self, store: PostgresStore, card: str, uid: str, online: dict[str, Any]
    ) -> None:
        from src.features.feast_store import card_fields, online_digest, uid_fields

        actual = (
            online_digest(online, uid_fields()),
            online_digest(online, card_fields(self.bundle.history_config.change_columns)),
        )
        if actual != store.feast_digests(card, uid):
            raise FeastHistoryUnavailableError("Redis feature row differs from its checkpoint")

    def recover_pending_feast(self) -> None:
        """Retry work left by a previous writer, even if a card receives no new events."""
        if self.feast is None or not isinstance(self.store, PostgresStore):
            return
        cards = self.store.pending_feast_cards()
        if cards:
            logger.info("recovering pending Feast updates count=%d", len(cards))
        for card_id in cards:
            with self.store.serialize_card(card_id):
                self._check_generation(self.store)
                self._recover_feast(self.store, card_id)

    def _push_pending(
        self,
        store: PostgresStore,
        card_id: str,
        uid: str,
        event: dict[str, Any],
        online: dict[str, Any],
    ) -> None:
        assert self.feast is not None
        try:
            # The saved pre-update state yields the same values and write timestamps on retry.
            self.feast.push(event, card_id, uid, online)
            if self.feast_generation is None:
                store.finish_feast(card_id, int(event["TransactionID"]))
            else:
                from src.features.feast_store import card_fields, online_digest, uid_fields

                fields = card_fields(self.bundle.history_config.change_columns)
                user_state, card_state = self.feast.next_rows(event, card_id, uid, online)
                user_digest = online_digest(user_state, uid_fields())
                card_digest = online_digest(card_state, fields)
                if (
                    user_digest is None
                    or card_digest is None
                    or self.feast.entity_digest("uid", uid) != user_digest
                    or self.feast.entity_digest("card", card_id) != card_digest
                ):
                    raise FeastHistoryUnavailableError("Redis did not apply the pending state")
                store.finish_feast(
                    card_id,
                    int(event["TransactionID"]),
                    digests=[("uid", uid, user_digest), ("card", card_id, card_digest)],
                )
        except Exception as exc:
            logger.warning("feast update pending error=%s", type(exc).__name__)
            raise PendingFeastUpdateError("Feature-state update pending") from None

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
        except Exception as exc:
            logger.error("feast update failed error=%s; rerun materialize", type(exc).__name__)
