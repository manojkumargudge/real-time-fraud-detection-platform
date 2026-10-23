"""Kafka consumer: validate -> score -> persist (or dead-letter) -> commit offset.

Usage: ``python -m src.streaming.consumer [--max-messages N] [--idle-timeout S]``
(env: ``KAFKA_BOOTSTRAP_SERVERS``, ``KAFKA_TOPIC``, ``KAFKA_GROUP_ID``, ``KAFKA_DLQ_TOPIC``,
``MODEL_DIR``, ``DATABASE_URL``).

Offset safety: a message's offset is committed only after its result is persisted or its DLQ
record is confirmed. Anything that interrupts a message -- shutdown, a rebalance, a crash --
leaves it uncommitted, so it is redelivered; the ``TransactionID`` primary key makes the
redelivery a no-op. Retry and DLQ rules are in ``reliability.py``; operations in
docs/serving.md ("Reliability").
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from collections import Counter
from typing import Any

from confluent_kafka import Consumer, KafkaError, KafkaException, TopicPartition
from confluent_kafka.admin import AdminClient

from src.serving.config import Settings
from src.serving.scorer import Scorer
from src.streaming.processor import ProcessOutcome, process_message
from src.streaming.reliability import (
    DeadLetterError,
    DeadLetterSink,
    KafkaDeadLetterSink,
    Reason,
    RetryPolicy,
    build_dlq_producer,
    classify,
    dead_letter,
    ensure_dlq_topic,
)

logger = logging.getLogger(__name__)

# The group moved on (rebalance): the message stays uncommitted and is redelivered to the
# partition's owner, where the primary key deduplicates it.
REBALANCE_COMMIT_ERRORS = frozenset(
    {KafkaError.ILLEGAL_GENERATION, KafkaError.UNKNOWN_MEMBER_ID, KafkaError.REBALANCE_IN_PROGRESS}
)


def build_consumer(settings: Settings) -> Consumer:
    return Consumer(
        {
            "bootstrap.servers": settings.kafka_bootstrap_servers,
            "group.id": settings.kafka_group_id,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )


def build_dead_letter_sink(settings: Settings) -> DeadLetterSink:
    """Provision the DLQ topic (30-day retention) before any write, then return its sink."""
    ensure_dlq_topic(
        AdminClient({"bootstrap.servers": settings.kafka_bootstrap_servers}),
        settings.kafka_dlq_topic,
    )
    return KafkaDeadLetterSink(
        build_dlq_producer(settings.kafka_bootstrap_servers), settings.kafka_dlq_topic
    )


class _MessageAbandonedError(Exception):
    """Stop requested or the in-flight partition was revoked: leave the message uncommitted."""


class _Worker:
    """Processes one message at a time with retries; never commits."""

    def __init__(
        self,
        consumer: Consumer,
        scorer: Scorer,
        dead_letters: DeadLetterSink,
        policy: RetryPolicy,
        stop: threading.Event | None,
        counts: Counter[str],
    ) -> None:
        self.consumer, self.scorer, self.dead_letters = consumer, scorer, dead_letters
        self.policy, self.stop, self.counts = policy, stop, counts
        self.revoked: set[tuple[str, int]] = set()  # revoked while the current message ran

    def on_assign(self, _consumer: Any, partitions: list[TopicPartition]) -> None:
        logger.info("partitions assigned: %s", _partitions(partitions))

    def on_revoke(self, _consumer: Any, partitions: list[TopicPartition]) -> None:
        self.revoked.update((p.topic, p.partition) for p in partitions)
        logger.info("partitions revoked: %s", _partitions(partitions))

    def handle(self, msg: Any) -> ProcessOutcome:
        self.revoked.clear()
        attempt = 0
        while True:
            attempt += 1
            try:
                outcome = process_message(msg.value(), self.scorer)
            except Exception as exc:  # classified: transient -> wait, permanent -> retry/DLQ
                kind = classify(exc)
                if kind == "permanent" and attempt > self.policy.max_retries:
                    self._dead_letter(msg, "processing_error", attempt, exc)
                    return ProcessOutcome(status="failed")
                self.counts["retries"] += 1
                logger.warning(
                    "processing failed kind=%s attempt=%d error=%s %s",
                    kind,
                    attempt,
                    type(exc).__name__,
                    _where(msg),
                )
                self._backoff(self.policy.delay(attempt), msg)
                continue
            if outcome.status == "invalid":
                self._dead_letter(msg, "invalid", attempt, outcome.cause)
            elif outcome.status == "duplicate":
                logger.info("duplicate id=%s skipped %s", outcome.transaction_id, _where(msg))
            return outcome

    def _dead_letter(
        self, msg: Any, reason: Reason, attempts: int, exc: BaseException | None
    ) -> None:
        letter = dead_letter(msg, reason, attempts, exc)
        failures = 0
        while True:
            try:
                self.dead_letters.send(letter)
            except DeadLetterError as err:  # DLQ unavailable: retry, never commit before it
                failures += 1
                logger.error("DLQ write failed attempt=%d: %s %s", failures, err, _where(msg))
                self._backoff(self.policy.delay(failures), msg)
                continue
            self.counts["dead_lettered"] += 1
            logger.error(
                "dead-lettered reason=%s attempts=%d error=%s %s",
                reason,
                attempts,
                type(exc).__name__ if exc else "-",
                _where(msg),
            )
            return

    def _backoff(self, seconds: float, msg: Any) -> None:
        """Wait while staying in the group: partitions paused, ``poll`` keeps serving the
        rebalance callbacks and the max.poll.interval deadline."""
        tp = (msg.topic(), msg.partition())
        self.consumer.pause(self.consumer.assignment())
        try:
            deadline = time.monotonic() + seconds
            while True:
                if self.stop is not None and self.stop.is_set():
                    raise _MessageAbandonedError("stop requested")
                if tp in self.revoked:
                    raise _MessageAbandonedError("partition revoked")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                polled = self.consumer.poll(min(remaining, 1.0))
                if polled is not None and not polled.error():
                    # Paused partitions should not deliver; rewind anything that slipped through.
                    _rewind(self.consumer, polled)
        finally:
            self.consumer.resume(self.consumer.assignment())


def _rewind(consumer: Consumer, msg: Any) -> None:
    try:
        consumer.seek(TopicPartition(msg.topic(), msg.partition(), msg.offset()))
    except KafkaException as exc:  # partition no longer assigned: its owner re-reads it
        logger.warning("could not rewind %s: %s", _where(msg), exc)


def _where(msg: Any) -> str:
    return f"topic={msg.topic()} partition={msg.partition()} offset={msg.offset()}"


def _partitions(partitions: list[TopicPartition]) -> str:
    """``transactions[0, 1, 2]``; one entry per topic."""
    by_topic: dict[str, list[int]] = {}
    for p in partitions:
        by_topic.setdefault(p.topic, []).append(p.partition)
    return " ".join(f"{t}{sorted(ps)}" for t, ps in sorted(by_topic.items())) or "none"


def run(
    consumer: Consumer,
    scorer: Scorer,
    topic: str,
    *,
    dead_letters: DeadLetterSink,
    policy: RetryPolicy | None = None,
    max_messages: int | None = None,
    idle_timeout: float | None = None,
    stop: threading.Event | None = None,
) -> Counter[str]:
    """Consume until ``max_messages`` processed, no message for ``idle_timeout`` seconds, or
    ``stop`` is set (checked between messages and during retry waits; an interrupted message
    is left uncommitted).
    """
    counts: Counter[str] = Counter()
    worker = _Worker(consumer, scorer, dead_letters, policy or RetryPolicy(), stop, counts)
    processed = 0  # counts also holds "alerts"/"retries", so it cannot be summed for this
    consumer.subscribe([topic], on_assign=worker.on_assign, on_revoke=worker.on_revoke)
    last_message = time.monotonic()
    try:
        while max_messages is None or processed < max_messages:
            if stop is not None and stop.is_set():
                logger.info("stop requested; leaving the consumer group")
                break
            msg = consumer.poll(1.0)
            if msg is None:
                if idle_timeout is not None and time.monotonic() - last_message > idle_timeout:
                    break
                continue
            if msg.error():
                code = msg.error().code()
                if code == KafkaError._PARTITION_EOF:
                    continue
                if code == KafkaError.UNKNOWN_TOPIC_OR_PART:  # created on first produce
                    logger.warning("topic not available yet topic=%s; waiting", topic)
                    continue
                raise KafkaException(msg.error())
            last_message = time.monotonic()
            try:
                outcome = worker.handle(msg)
            except _MessageAbandonedError as why:
                logger.info("left uncommitted (%s) %s", why, _where(msg))
                counts["interrupted"] += 1
                continue  # stop -> loop exits at the top; revoked -> next poll
            try:
                consumer.commit(message=msg, asynchronous=False)  # after DB write or DLQ ack
            except KafkaException as exc:
                if exc.args[0].code() not in REBALANCE_COMMIT_ERRORS:
                    raise
                logger.warning(
                    "commit rejected by a rebalance; will be redelivered %s", _where(msg)
                )
                counts["commit_rejected"] += 1
            counts[outcome.status] += 1
            processed += 1
            if outcome.is_alert and outcome.status == "scored":
                counts["alerts"] += 1
    finally:
        consumer.close()
    logger.info("consumer stopped counts=%s", dict(counts))
    return counts


def main(argv: list[str] | None = None) -> Counter[str]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-messages", type=int, default=None)
    parser.add_argument("--idle-timeout", type=float, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    scorer = settings.build_scorer()

    # `docker stop` sends SIGTERM; without a handler Python exits at once, skipping
    # consumer.close() (so the group waits for a session timeout before rebalancing).
    stop = threading.Event()

    def request_stop(signum: int, _frame: object) -> None:
        logger.info("received signal %d; stopping after the current message", signum)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        return run(
            build_consumer(settings),
            scorer,
            settings.kafka_topic,
            dead_letters=build_dead_letter_sink(settings),
            max_messages=args.max_messages,
            idle_timeout=args.idle_timeout,
            stop=stop,
        )
    finally:
        scorer.store.close()


if __name__ == "__main__":
    main()
