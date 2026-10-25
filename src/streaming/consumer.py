"""Kafka consumer: validate -> score -> persist (or dead-letter) -> commit offset.

Usage: ``python -m src.streaming.consumer [--max-messages N] [--idle-timeout S]``
(env: ``KAFKA_BOOTSTRAP_SERVERS``, ``KAFKA_TOPIC``, ``KAFKA_GROUP_ID``, ``KAFKA_DLQ_TOPIC``,
``MODEL_DIR``, ``DATABASE_URL``; ``STAGE_TIMINGS=1`` logs per-stage timings per message;
``CONSUMER_HEARTBEAT_FILE`` enables the liveness heartbeat file, see ``Heartbeat``).

Offset safety: a message's offset is committed only after its result is persisted or its DLQ
record is confirmed. Anything that interrupts a message -- shutdown, a rebalance, a crash --
leaves it uncommitted, so it is redelivered; the ``TransactionID`` primary key makes the
redelivery a no-op. Retry and DLQ rules are in ``reliability.py``; operations in
docs/serving.md ("Reliability").

Commit errors: a rebalance rejection is accepted at once (the group moved on). Retriable
coordinator errors (``NOT_COORDINATOR`` after a broker restart, ...) are retried a few times;
if the commit still fails the message is left for redelivery and the consumer keeps running.
Anything else raises.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from confluent_kafka import Consumer, KafkaError, KafkaException, TopicPartition
from confluent_kafka.admin import AdminClient

from src import timing
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
# The group coordinator moved, is loading or is unreachable (broker restart, heavy load): retry
# the commit; if it still fails, the message is redelivered later. Also any error that
# librdkafka flags as retriable (``KafkaError.retriable()``).
RETRIABLE_COMMIT_ERRORS = frozenset(
    {
        KafkaError.NOT_COORDINATOR,
        KafkaError.COORDINATOR_LOAD_IN_PROGRESS,
        KafkaError.COORDINATOR_NOT_AVAILABLE,
        KafkaError.REQUEST_TIMED_OUT,
        KafkaError._TIMED_OUT,
    }
)
COMMIT_RETRY_DELAYS = (0.2, 0.5, 1.0)  # seconds before retries 1, 2 and 3
COMMIT_FAILURE_ALERT_S = 30.0  # failing longer than this: ERROR, repeated at this interval


def is_retriable_commit_error(err: KafkaError) -> bool:
    return err.code() in RETRIABLE_COMMIT_ERRORS or bool(err.retriable())


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


class Heartbeat:
    """Liveness file for a Kubernetes exec probe (docs/kubernetes.md), off unless a path is set.

    Touched on every poll-loop iteration (also when idle) and during retry waits, so it goes
    stale only when the loop itself stops (hung or frozen process), not while the consumer is
    waiting out a database or DLQ outage.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._warned = False

    def beat(self) -> None:
        if self.path is None:
            return
        try:
            self.path.touch()
        except OSError as exc:  # never let the probe file stop consumption
            if not self._warned:
                logger.warning("cannot update heartbeat file %s: %s", self.path, exc)
                self._warned = True


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
        heartbeat: Heartbeat | None = None,
    ) -> None:
        self.consumer, self.scorer, self.dead_letters = consumer, scorer, dead_letters
        self.policy, self.stop, self.counts = policy, stop, counts
        self.heartbeat = heartbeat or Heartbeat()
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
                self.heartbeat.beat()
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


class _Committer:
    """Synchronous offset commit that survives rebalances and coordinator outages.

    A commit that is not accepted leaves the message uncommitted: it is redelivered (to this
    or another consumer) and the primary key deduplicates it, so delivery stays at-least-once.
    ``counts["consecutive_commit_failures"]`` is a gauge, not a total: commits that failed in a
    row after all retries, reset by the next successful commit.
    """

    def __init__(
        self,
        consumer: Consumer,
        counts: Counter[str],
        *,
        delays: tuple[float, ...] = COMMIT_RETRY_DELAYS,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.consumer, self.counts, self.delays = consumer, counts, delays
        self.sleep, self.clock = sleep, clock
        self.failing_since: float | None = None  # first failed commit of the current streak
        self.last_alert: float | None = None

    def commit(self, msg: Any) -> bool:
        """True if the offset was committed; raises for errors that are not retriable."""
        for attempt in range(1, len(self.delays) + 2):
            if attempt > 1:
                self.counts["commit_retries"] += 1
                self.sleep(self.delays[attempt - 2])
            try:
                self.consumer.commit(message=msg, asynchronous=False)  # after DB write or DLQ ack
            except KafkaException as exc:
                err: KafkaError = exc.args[0]
                if err.code() in REBALANCE_COMMIT_ERRORS:
                    logger.warning(
                        "commit rejected by a rebalance; will be redelivered %s", _where(msg)
                    )
                    self.counts["commit_rejected"] += 1
                    return False
                if not is_retriable_commit_error(err):
                    raise
                if self.failing_since is None:
                    self.failing_since = self.clock()
                logger.warning(
                    "commit failed attempt=%d/%d error=%s %s",
                    attempt,
                    len(self.delays) + 1,
                    err.name(),
                    _where(msg),
                )
                continue
            self._succeeded()
            return True
        self._gave_up(msg)
        return False

    def _gave_up(self, msg: Any) -> None:
        self.counts["commit_rejected"] += 1
        self.counts["consecutive_commit_failures"] += 1
        logger.warning("commit not accepted after retries; will be redelivered %s", _where(msg))
        now = self.clock()
        failing_for = now - (self.failing_since if self.failing_since is not None else now)
        if failing_for > COMMIT_FAILURE_ALERT_S and (
            self.last_alert is None or now - self.last_alert >= COMMIT_FAILURE_ALERT_S
        ):
            self.last_alert = now
            logger.error(
                "offset commits failing for %.0f s consecutive_commit_failures=%d; "
                "messages are redelivered once the coordinator is back",
                failing_for,
                self.counts["consecutive_commit_failures"],
            )

    def _succeeded(self) -> None:
        if self.failing_since is not None:
            logger.info(
                "offset commits recovered after %.1f s consecutive_commit_failures=%d",
                self.clock() - self.failing_since,
                self.counts["consecutive_commit_failures"],
            )
        self.failing_since = self.last_alert = None
        self.counts.pop("consecutive_commit_failures", None)


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
    commit_delays: tuple[float, ...] = COMMIT_RETRY_DELAYS,
    stage_timings: bool = False,
    heartbeat: Heartbeat | None = None,
) -> Counter[str]:
    """Consume until ``max_messages`` processed, no message for ``idle_timeout`` seconds, or
    ``stop`` is set (checked between messages and during retry waits; an interrupted message
    is left uncommitted).

    ``stage_timings`` logs one ``stages {...}`` line per message with the milliseconds spent in
    each stage (``poll`` = the call that returned the message, ``process`` = validate + score +
    persist, ``commit``, plus the scorer's stages; see ``src.timing``).

    ``heartbeat`` is beaten on every loop iteration (see ``Heartbeat``).
    """
    counts: Counter[str] = Counter()
    beat = heartbeat or Heartbeat()
    worker = _Worker(consumer, scorer, dead_letters, policy or RetryPolicy(), stop, counts, beat)
    committer = _Committer(consumer, counts, delays=commit_delays)
    processed = 0  # counts also holds "alerts"/"retries", so it cannot be summed for this
    consumer.subscribe([topic], on_assign=worker.on_assign, on_revoke=worker.on_revoke)
    last_message = time.monotonic()
    try:
        while max_messages is None or processed < max_messages:
            beat.beat()
            if stop is not None and stop.is_set():
                logger.info("stop requested; leaving the consumer group")
                break
            polled_at = time.perf_counter()
            msg = consumer.poll(1.0)
            poll_ms = (time.perf_counter() - polled_at) * 1000
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
            stages = timing.collect() if stage_timings else None
            try:
                with timing.stage("process"):
                    outcome = worker.handle(msg)
            except _MessageAbandonedError as why:
                logger.info("left uncommitted (%s) %s", why, _where(msg))
                counts["interrupted"] += 1
                continue  # stop -> loop exits at the top; revoked -> next poll
            finally:
                timing.clear()
            committed_at = time.perf_counter()
            committer.commit(msg)
            if stages is not None:
                stages["poll"] = poll_ms
                stages["commit"] = (time.perf_counter() - committed_at) * 1000
                logger.info(
                    "stages id=%s status=%s %s",
                    outcome.transaction_id,
                    outcome.status,
                    json.dumps({k: round(v, 3) for k, v in stages.items()}),
                )
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
            stage_timings=os.environ.get("STAGE_TIMINGS") == "1",
            heartbeat=Heartbeat(Path(hb))
            if (hb := os.environ.get("CONSUMER_HEARTBEAT_FILE"))
            else None,
        )
    finally:
        scorer.store.close()


if __name__ == "__main__":
    main()
