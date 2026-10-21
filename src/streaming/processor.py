"""Broker-independent message handling used by the Kafka consumer.

Delivery semantics: the consumer commits an offset only after ``process_message`` returns,
i.e. after the database write. A crash before the commit re-delivers the message
(at-least-once); the ``TransactionID`` primary key turns the redelivery into a no-op, so the
stored result is written once per ID. This is idempotent at-least-once processing, not
Kafka exactly-once semantics.

Invalid messages (bad JSON or schema) are logged and skipped so one poison message cannot
block the partition. Infrastructure errors (e.g. database down) propagate, so the offset
is not committed and the message is retried after restart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from src.serving.scorer import Scorer
from src.streaming.events import decode

logger = logging.getLogger(__name__)

Status = Literal["scored", "duplicate", "invalid"]


@dataclass(frozen=True)
class ProcessOutcome:
    status: Status
    transaction_id: int | None = None
    score: float | None = None
    is_alert: bool | None = None
    error: str | None = None


def process_message(value: bytes | None, scorer: Scorer) -> ProcessOutcome:
    try:
        event = decode(value)
    except (ValueError, ValidationError) as exc:
        error = str(exc).splitlines()[0][:300]
        logger.warning("skipping invalid message error=%s", error)
        return ProcessOutcome(status="invalid", error=error)
    result = scorer.score_event(event)
    return ProcessOutcome(
        status="duplicate" if result.duplicate else "scored",
        transaction_id=result.transaction_id,
        score=result.score,
        is_alert=result.is_alert,
    )
