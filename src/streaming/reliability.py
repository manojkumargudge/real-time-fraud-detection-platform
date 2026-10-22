"""Retry policy, error classification and dead-letter (DLQ) delivery for the consumer.

Error classes (see ``classify``):

* ``invalid``   -- bad JSON or schema: deterministic, dead-lettered on the first attempt.
* ``transient`` -- database/network unavailable: retried with capped backoff *indefinitely*
  and never dead-lettered, so an outage pauses consumption instead of skipping events or
  flooding the DLQ.
* ``permanent`` -- any other processing error: retried ``max_retries`` times after the first
  attempt, then dead-lettered.

A DLQ record keeps the original key, value bytes and headers unchanged and adds ``dlq.*``
headers. Error metadata is sanitised: exception type, validation field locations and SQLSTATE
only -- never exception messages, which can echo card or address fields.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import psycopg
from confluent_kafka import KafkaError, KafkaException, Producer
from confluent_kafka.admin import AdminClient, ConfigResource, NewTopic
from pydantic import ValidationError

logger = logging.getLogger(__name__)

DLQ_RETENTION_DAYS = 30
DLQ_RETENTION_MS = DLQ_RETENTION_DAYS * 24 * 3600 * 1000
DLQ_PARTITIONS = 1
DLQ_REPLICATION = 1  # single-broker development stack

ErrorKind = Literal["transient", "permanent"]
Reason = Literal["invalid", "processing_error"]

# Connection loss, server shutdown, timeouts, serialization/deadlock aborts (all retryable).
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    psycopg.OperationalError,
    psycopg.InterfaceError,
    ConnectionError,
    TimeoutError,
)


def classify(exc: BaseException) -> ErrorKind:
    return "transient" if isinstance(exc, TRANSIENT_ERRORS) else "permanent"


@dataclass(frozen=True)
class RetryPolicy:
    """Capped exponential backoff with "equal jitter": delay in ``[d/2, d]``."""

    max_retries: int = 3  # after the initial attempt, for permanent errors
    base_delay: float = 0.5
    max_delay: float = 30.0
    rng: random.Random = field(default_factory=random.Random, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_retries < 0 or self.base_delay < 0 or self.max_delay < self.base_delay:
            raise ValueError("need max_retries >= 0 and 0 <= base_delay <= max_delay")

    def delay(self, attempt: int) -> float:
        """Seconds to wait after failed attempt number ``attempt`` (1-based)."""
        if attempt < 1:
            raise ValueError("attempt is 1-based")
        capped = min(self.max_delay, self.base_delay * 2.0 ** min(attempt - 1, 32))
        return capped / 2 + self.rng.uniform(0, capped / 2)


def sanitize_error(exc: BaseException) -> dict[str, str]:
    """Exception metadata safe to store: no messages, no input values."""
    detail = ""
    if isinstance(exc, ValidationError):
        detail = ";".join(f"{'.'.join(map(str, e['loc']))}:{e['type']}" for e in exc.errors())
    elif isinstance(exc, psycopg.Error):
        detail = f"sqlstate={exc.sqlstate or 'none'}"
    elif isinstance(exc.__cause__, json.JSONDecodeError):
        detail = f"json:{exc.__cause__.msg} at char {exc.__cause__.pos}"  # fixed template text
    return {"dlq.error.type": type(exc).__name__, "dlq.error.detail": detail[:500]}


@dataclass(frozen=True)
class DeadLetter:
    key: bytes | None
    value: bytes | None
    headers: list[tuple[str, bytes]]


def dead_letter(
    msg: Any,
    reason: Reason,
    attempts: int,
    exc: BaseException | None,
    *,
    now: datetime | None = None,
) -> DeadLetter:
    """Build the DLQ record for Kafka message ``msg`` (original bytes and headers kept)."""
    ts_type, ts_ms = msg.timestamp()
    meta = {
        "dlq.reason": reason,
        "dlq.source.topic": msg.topic(),
        "dlq.source.partition": str(msg.partition()),
        "dlq.source.offset": str(msg.offset()),
        "dlq.source.timestamp_ms": str(ts_ms),
        "dlq.attempts": str(attempts),
        "dlq.failed_at": (now or datetime.now(UTC)).isoformat(timespec="milliseconds"),
        **(sanitize_error(exc) if exc is not None else {}),
    }
    original = [
        (k, v if isinstance(v, bytes) else (v or "").encode()) for k, v in msg.headers() or []
    ]
    return DeadLetter(
        key=msg.key(),
        value=msg.value(),
        headers=[*original, *((k, v.encode()) for k, v in meta.items())],
    )


class DeadLetterError(RuntimeError):
    """The DLQ write was not confirmed; the source offset must not be committed."""


class DeadLetterSink(Protocol):
    def send(self, letter: DeadLetter) -> None:
        """Return only once delivery is confirmed; raise ``DeadLetterError`` otherwise."""


class KafkaDeadLetterSink:
    def __init__(self, producer: Producer, topic: str, *, timeout: float = 10.0) -> None:
        self.producer, self.topic, self.timeout = producer, topic, timeout

    def send(self, letter: DeadLetter) -> None:
        errors: list[str] = []

        def on_delivery(err: Any, _msg: Any) -> None:
            if err is not None:
                errors.append(str(err))

        try:
            self.producer.produce(
                self.topic,
                key=letter.key,
                value=letter.value,
                headers=letter.headers,
                on_delivery=on_delivery,
            )
        except (BufferError, KafkaException) as exc:  # BufferError: local queue full
            raise DeadLetterError(f"produce failed: {type(exc).__name__}") from exc
        if self.producer.flush(self.timeout):
            # Drop the still-queued copy so a retry does not queue a second one; a copy already
            # in flight may still arrive (the DLQ is at-least-once, keyed by dlq.source.*).
            self.producer.purge(in_queue=True, in_flight=False)
            raise DeadLetterError(f"delivery not confirmed within {self.timeout}s")
        if errors:
            raise DeadLetterError(f"delivery failed: {errors[0]}")


def build_dlq_producer(bootstrap_servers: str) -> Producer:
    # Idempotent producer: broker-side retries cannot duplicate or reorder a DLQ record.
    return Producer(
        {"bootstrap.servers": bootstrap_servers, "acks": "all", "enable.idempotence": True}
    )


def ensure_dlq_topic(admin: AdminClient, topic: str, *, timeout: float = 30.0) -> str:
    """Create ``topic`` with 30-day retention if absent; return its effective retention.ms.

    Must run before the first DLQ write: broker auto-creation would apply the default retention.
    An existing topic is never altered; a different retention is logged for operators.
    """
    if topic not in admin.list_topics(timeout=timeout).topics:
        new = NewTopic(
            topic,
            num_partitions=DLQ_PARTITIONS,
            replication_factor=DLQ_REPLICATION,
            config={"retention.ms": str(DLQ_RETENTION_MS), "cleanup.policy": "delete"},
        )
        try:
            admin.create_topics([new])[topic].result(timeout)
            logger.info("created DLQ topic=%s retention_ms=%d", topic, DLQ_RETENTION_MS)
        except KafkaException as exc:  # a concurrent consumer may have created it first
            if exc.args[0].code() != KafkaError.TOPIC_ALREADY_EXISTS:
                raise
    retention = _describe_retention(admin, topic, timeout)
    if retention != str(DLQ_RETENTION_MS):
        logger.warning(
            "DLQ topic=%s retention_ms=%s differs from the expected %d; not altered",
            topic,
            retention,
            DLQ_RETENTION_MS,
        )
    return retention


def _describe_retention(admin: AdminClient, topic: str, timeout: float) -> str:
    resource = ConfigResource(ConfigResource.Type.TOPIC, topic)
    deadline = time.monotonic() + timeout
    while True:
        try:
            configs = admin.describe_configs([resource])[resource].result(timeout)
            return str(configs["retention.ms"].value)
        except KafkaException as exc:
            # create_topics() resolves on the controller; the broker answering this request
            # can briefly not know the new topic yet (observed against the live broker).
            if (
                exc.args[0].code() != KafkaError.UNKNOWN_TOPIC_OR_PART
                or time.monotonic() > deadline
            ):
                raise
            time.sleep(0.2)
