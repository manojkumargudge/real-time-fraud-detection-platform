"""Retry, dead-letter and offset-safety rules of the Kafka consumer (no broker needed)."""

from __future__ import annotations

import random
import threading
from collections import Counter
from concurrent.futures import Future
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from confluent_kafka import KafkaError, KafkaException
from confluent_kafka.admin import ConfigResource, NewTopic
from pydantic import ValidationError

from src.serving.model import ModelBundle
from src.serving.schema import ScoreResponse, TransactionEvent
from src.serving.scorer import Scorer
from src.serving.store import InMemoryStore
from src.streaming import consumer as consumer_mod
from src.streaming.events import decode, encode
from src.streaming.reliability import (
    DLQ_RETENTION_MS,
    RetryPolicy,
    classify,
    dead_letter,
    ensure_dlq_topic,
    sanitize_error,
)
from tests.kafka_fakes import NO_WAIT, FakeConsumer, FakeDeadLetterSink, FakeMessage
from tests.test_serving import event

CARD = 4_111_111_111  # a value that must never reach DLQ metadata or logs


@pytest.fixture(scope="module")
def bundle(bundle_dir: Path) -> ModelBundle:
    return ModelBundle.load(bundle_dir)


class FlakyScorer:
    """Raises the queued exceptions in order (per call), then scores successfully."""

    def __init__(self, *failures: BaseException) -> None:
        self.failures = list(failures)
        self.calls = 0
        self.scored: list[int] = []

    def score_event(self, ev: TransactionEvent) -> ScoreResponse:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        self.scored.append(ev.TransactionID)
        return ScoreResponse(
            transaction_id=ev.TransactionID,
            score=0.1,
            is_alert=False,
            threshold=0.5,
            model_version="test",
            duplicate=False,
            history_rows=0,
            latency_ms=0.0,
        )


def values(*ids: int) -> list[bytes | None]:
    return [encode(event(TransactionID=i, TransactionDT=100_000 + i))[1] for i in ids]


def run(
    fake: FakeConsumer, scorer: object, dlq: FakeDeadLetterSink, **kw: object
) -> dict[str, int]:
    kw.setdefault("policy", NO_WAIT)
    kw.setdefault("idle_timeout", 0)
    return dict(consumer_mod.run(fake, scorer, "t", dead_letters=dlq, **kw))  # type: ignore[arg-type]


def offsets(fake: FakeConsumer) -> list[int]:
    return [m.offset() for m in fake.committed]


class TestRetryPolicy:
    def test_defaults_match_the_requirements(self) -> None:
        p = RetryPolicy()
        assert (p.max_retries, p.base_delay, p.max_delay) == (3, 0.5, 30.0)
        assert DLQ_RETENTION_MS == 30 * 24 * 60 * 60 * 1000

    def test_capped_exponential_backoff_with_bounded_jitter(self) -> None:
        p = RetryPolicy(base_delay=0.5, max_delay=4.0, rng=random.Random(1))
        for attempt, cap in [(1, 0.5), (2, 1.0), (3, 2.0), (4, 4.0), (5, 4.0), (500, 4.0)]:
            delays = [p.delay(attempt) for _ in range(200)]
            assert all(cap / 2 <= d <= cap for d in delays)
            assert len(set(delays)) > 1  # jittered

    def test_seeded_jitter_is_reproducible(self) -> None:
        a, b = RetryPolicy(rng=random.Random(7)), RetryPolicy(rng=random.Random(7))
        assert [a.delay(n) for n in range(1, 6)] == [b.delay(n) for n in range(1, 6)]

    @pytest.mark.parametrize("kwargs", [{"max_retries": -1}, {"base_delay": 2.0, "max_delay": 1.0}])
    def test_invalid_settings(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ValueError):
            RetryPolicy(**kwargs)  # type: ignore[arg-type]

    def test_attempts_are_one_based(self) -> None:
        with pytest.raises(ValueError):
            RetryPolicy().delay(0)


class TestClassification:
    @pytest.mark.parametrize(
        "exc",
        [
            psycopg.OperationalError("down"),
            psycopg.InterfaceError("closed"),
            ConnectionError(),
            TimeoutError(),
        ],
    )
    def test_transient(self, exc: BaseException) -> None:
        assert classify(exc) == "transient"

    @pytest.mark.parametrize(
        "exc",
        [
            psycopg.DataError("bad"),
            psycopg.IntegrityError("fk"),
            ValueError(),
            RuntimeError(),
            KeyError(),
        ],
    )
    def test_permanent(self, exc: BaseException) -> None:
        assert classify(exc) == "permanent"


class TestSanitizedErrors:
    def test_validation_errors_keep_field_locations_not_values(self) -> None:
        with pytest.raises(ValidationError) as info:
            TransactionEvent.model_validate(event(card1=CARD * 10**20, TransactionAmt=-5.25))
        meta = sanitize_error(info.value)
        assert meta["dlq.error.type"] == "ValidationError"
        assert "card1:less_than_equal" in meta["dlq.error.detail"]
        assert "TransactionAmt:greater_than_equal" in meta["dlq.error.detail"]
        assert str(CARD) not in str(meta) and "5.25" not in str(meta)

    def test_database_errors_keep_sqlstate_only(self) -> None:
        exc = psycopg.errors.UntranslatableCharacter(f"cannot store card {CARD}")
        assert sanitize_error(exc) == {
            "dlq.error.type": "UntranslatableCharacter",
            "dlq.error.detail": "sqlstate=22P05",
        }

    def test_json_errors_keep_the_parser_position_only(self) -> None:
        with pytest.raises(ValueError) as info:
            decode(b'{"card1": 4111111111, oops}')
        meta = sanitize_error(info.value)
        assert meta["dlq.error.detail"].startswith("json:") and str(CARD) not in str(meta)

    def test_other_errors_keep_the_type_only(self) -> None:
        assert sanitize_error(RuntimeError(f"card {CARD}")) == {
            "dlq.error.type": "RuntimeError",
            "dlq.error.detail": "",
        }


class TestDeadLetterRecord:
    def test_original_message_and_source_metadata_are_preserved(self) -> None:
        msg = FakeMessage(
            b'{"raw": true}',
            topic="transactions",
            partition=2,
            offset=41,
            key=b"card-key",
            headers=[("schema_version", b"1")],
        )
        now = datetime(2026, 10, 22, 8, 30, tzinfo=UTC)
        letter = dead_letter(msg, "processing_error", 4, RuntimeError("x"), now=now)
        assert (letter.key, letter.value) == (b"card-key", b'{"raw": true}')
        h = dict(letter.headers)
        assert h["schema_version"] == b"1"  # original headers first, unchanged
        assert h["dlq.source.topic"] == b"transactions"
        assert (h["dlq.source.partition"], h["dlq.source.offset"]) == (b"2", b"41")
        assert h["dlq.source.timestamp_ms"] == b"1700000000041"
        assert (h["dlq.attempts"], h["dlq.reason"]) == (b"4", b"processing_error")
        assert h["dlq.failed_at"] == b"2026-10-22T08:30:00.000+00:00"
        assert h["dlq.error.type"] == b"RuntimeError"


class TestConsumerReliability:
    def test_permanent_failure_is_dead_lettered_after_three_retries(self) -> None:
        fake, dlq = FakeConsumer(values(1, 2)), FakeDeadLetterSink()
        scorer = FlakyScorer(*(RuntimeError(f"card {CARD}") for _ in range(4)))
        counts = run(fake, scorer, dlq)
        assert scorer.calls == 5 and scorer.scored == [2]  # 1 + 3 retries for event 1, then event 2
        (letter,) = dlq.sent
        h = dict(letter.headers)
        assert (h["dlq.attempts"], h["dlq.reason"], h["dlq.source.offset"]) == (
            b"4",
            b"processing_error",
            b"0",
        )
        assert letter.value == fake.all[0].value()  # full original event
        assert offsets(fake) == [0, 1]  # committed after the DLQ ack, then the next message
        assert counts["retries"] == 3 and counts["scored"] == 1
        assert counts["failed"] == 1 and counts["dead_lettered"] == 1  # one status, one DLQ write

    def test_permanent_failure_that_recovers_within_the_retries_is_scored(self) -> None:
        fake, dlq = FakeConsumer(values(1)), FakeDeadLetterSink()
        counts = run(fake, FlakyScorer(RuntimeError(), RuntimeError()), dlq)
        assert (
            dlq.sent == []
            and offsets(fake) == [0]
            and counts["scored"] == 1
            and counts["retries"] == 2
        )

    def test_transient_outage_is_waited_out_without_dlq_or_skipping(self) -> None:
        outage = [psycopg.OperationalError("connection refused") for _ in range(12)]
        fake, dlq = FakeConsumer(values(1, 2)), FakeDeadLetterSink()
        scorer = FlakyScorer(*outage)
        counts = run(fake, scorer, dlq)
        assert dlq.sent == [] and counts["retries"] == 12  # far beyond max_retries=3
        assert scorer.scored == [1, 2] and offsets(fake) == [0, 1]  # order kept, nothing skipped
        assert fake.pause_calls == 12 and not fake.paused  # paused while waiting, then resumed

    def test_shutdown_during_an_outage_leaves_the_offset_for_redelivery(
        self, bundle: ModelBundle
    ) -> None:
        store, stop = InMemoryStore(), threading.Event()

        class Outage(FlakyScorer):
            def score_event(self, ev: TransactionEvent) -> ScoreResponse:
                if self.calls == 2:
                    stop.set()  # SIGTERM arrives during the outage
                return super().score_event(ev)

        first = FakeConsumer(values(7))
        run(
            first,
            Outage(*(psycopg.OperationalError() for _ in range(10))),
            FakeDeadLetterSink(),
            stop=stop,
        )
        assert first.committed == [] and first.closed

        # Restart: the uncommitted message is redelivered and stored exactly once.
        scorer = Scorer(bundle, store)
        again = FakeConsumer(values(7, 7))  # redelivery plus a duplicate copy
        counts = run(again, scorer, FakeDeadLetterSink())
        assert len(store) == 1 and offsets(again) == [0, 1]
        assert counts["scored"] == 1 and counts["duplicate"] == 1

    def test_dlq_failure_blocks_the_commit_until_delivery_is_confirmed(self) -> None:
        fake, dlq = FakeConsumer([b"{poison", *values(2)]), FakeDeadLetterSink(failures=3)
        counts = run(fake, FlakyScorer(), dlq)
        assert dlq.attempts == 4 and len(dlq.sent) == 1  # confirmed once, after 3 failures
        assert offsets(fake) == [0, 1] and counts["invalid"] == 1 and counts["dead_lettered"] == 1

    def test_dlq_outage_and_shutdown_leave_the_offset_uncommitted(self) -> None:
        stop = threading.Event()
        dlq = FakeDeadLetterSink(
            failures=10**9, on_attempt=lambda n: stop.set() if n == 3 else None
        )
        fake = FakeConsumer([b"{poison"])
        counts = run(fake, FlakyScorer(), dlq, stop=stop)
        assert fake.committed == [] and dlq.sent == [] and counts["interrupted"] == 1

    def test_rebalance_during_a_retry_abandons_the_message_uncommitted(self) -> None:
        fake, dlq = FakeConsumer(values(1)), FakeDeadLetterSink()
        fake.poll_hook = lambda c: c.revoke() if c.paused else None  # rebalance mid-backoff
        policy = RetryPolicy(base_delay=0.05, max_delay=0.05)
        counts = run(
            fake, FlakyScorer(*(psycopg.OperationalError() for _ in range(99))), dlq, policy=policy
        )
        assert fake.committed == [] and dlq.sent == [] and counts["interrupted"] == 1

    def test_messages_delivered_while_paused_are_rewound_not_lost(self) -> None:
        fake, dlq = FakeConsumer(values(1, 2)), FakeDeadLetterSink()
        fake.ignore_pause = True  # an already-fetched message slips through the pause
        policy = RetryPolicy(base_delay=0.02, max_delay=0.02)
        scorer = FlakyScorer(psycopg.OperationalError())
        run(fake, scorer, dlq, policy=policy)
        assert ("t", 0, 1) in fake.seeks  # message 2 rewound during the wait
        assert scorer.scored == [1, 2] and offsets(fake) == [0, 1]

    def test_commit_rejected_by_a_rebalance_is_not_fatal(self) -> None:
        fake, dlq = FakeConsumer(values(1, 2)), FakeDeadLetterSink()
        fake.commit_errors = [KafkaError.ILLEGAL_GENERATION]
        counts = run(fake, FlakyScorer(), dlq)
        assert counts["commit_rejected"] == 1 and offsets(fake) == [1] and counts["scored"] == 2

    def test_other_commit_errors_stop_the_consumer(self) -> None:
        fake = FakeConsumer(values(1))
        fake.commit_errors = [KafkaError._TRANSPORT]
        with pytest.raises(KafkaException):
            run(fake, FlakyScorer(), FakeDeadLetterSink())
        assert fake.closed

    @pytest.mark.parametrize(
        "error",
        [
            KafkaError.NOT_COORDINATOR,
            KafkaError.COORDINATOR_LOAD_IN_PROGRESS,
            KafkaError.COORDINATOR_NOT_AVAILABLE,
            KafkaError.REQUEST_TIMED_OUT,
            KafkaError._TIMED_OUT,
            KafkaError(KafkaError._TRANSPORT, retriable=True),  # flagged retriable by librdkafka
        ],
    )
    def test_retriable_commit_error_is_retried_then_counted_not_fatal(
        self, error: int | KafkaError
    ) -> None:
        fake, dlq = FakeConsumer(values(1, 2)), FakeDeadLetterSink()
        fake.commit_errors = [error] * 4  # first attempt + 3 retries for message 1
        counts = run(fake, FlakyScorer(), dlq, commit_delays=(0.0, 0.0, 0.0))
        assert fake.commit_calls == 5 and offsets(fake) == [1]  # message 2 commits normally
        assert counts["commit_retries"] == 3 and counts["commit_rejected"] == 1
        assert counts["scored"] == 2 and fake.closed
        assert "consecutive_commit_failures" not in counts  # reset by message 2's commit

    def test_commit_retry_that_succeeds_commits_the_message(self) -> None:
        fake, dlq = FakeConsumer(values(1, 2)), FakeDeadLetterSink()
        fake.commit_errors = [KafkaError.NOT_COORDINATOR]
        counts = run(fake, FlakyScorer(), dlq, commit_delays=(0.0, 0.0, 0.0))
        assert offsets(fake) == [0, 1] and fake.commit_calls == 3
        assert counts["commit_retries"] == 1 and "commit_rejected" not in counts

    @pytest.mark.parametrize(
        "code",
        [KafkaError._TRANSPORT, KafkaError.TOPIC_AUTHORIZATION_FAILED, KafkaError.INVALID_GROUP_ID],
    )
    def test_non_retriable_commit_error_raises_without_retry(self, code: int) -> None:
        fake = FakeConsumer(values(1))
        fake.commit_errors = [code]
        with pytest.raises(KafkaException):
            run(fake, FlakyScorer(), FakeDeadLetterSink(), commit_delays=(0.0, 0.0, 0.0))
        assert fake.commit_calls == 1 and fake.closed

    def test_non_retriable_error_during_commit_retries_raises(self) -> None:
        fake = FakeConsumer(values(1))
        fake.commit_errors = [KafkaError.NOT_COORDINATOR, KafkaError._TRANSPORT]
        with pytest.raises(KafkaException):
            run(fake, FlakyScorer(), FakeDeadLetterSink(), commit_delays=(0.0, 0.0, 0.0))

    def test_rebalance_during_commit_retries_stops_retrying(self) -> None:
        fake, dlq = FakeConsumer(values(1, 2)), FakeDeadLetterSink()
        fake.commit_errors = [KafkaError.NOT_COORDINATOR, KafkaError.ILLEGAL_GENERATION]
        counts = run(fake, FlakyScorer(), dlq, commit_delays=(0.0, 0.0, 0.0))
        assert fake.commit_calls == 3 and offsets(fake) == [1]
        assert counts["commit_rejected"] == 1 and counts["commit_retries"] == 1


class TestCommitter:
    """``_Committer`` alone, with a fake clock: retry backoff and the sustained-failure alarm."""

    @staticmethod
    def make() -> tuple[FakeConsumer, consumer_mod._Committer, list[float], list[float]]:
        fake, now, sleeps = FakeConsumer(values(*range(20))), [1_000.0], []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        committer = consumer_mod._Committer(fake, Counter(), sleep=sleep, clock=lambda: now[0])
        return fake, committer, now, sleeps

    def test_retries_use_the_backoff_schedule(self) -> None:
        fake, committer, _, sleeps = self.make()
        fake.commit_errors = [KafkaError.NOT_COORDINATOR] * 4
        assert committer.commit(fake.all[0]) is False
        assert sleeps == [0.2, 0.5, 1.0] and fake.commit_calls == 4
        assert committer.counts["commit_rejected"] == 1
        assert committer.counts["consecutive_commit_failures"] == 1

    def test_sustained_failures_log_an_error_every_30_s_and_count_up(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake, committer, now, _ = self.make()
        with caplog.at_level("INFO", logger="src.streaming.consumer"):
            for i in range(14):  # each failed commit takes 1.7 s of retries + 3.3 s of work
                fake.commit_errors = [KafkaError.COORDINATOR_LOAD_IN_PROGRESS] * 4
                assert committer.commit(fake.all[i]) is False
                assert committer.counts["consecutive_commit_failures"] == i + 1
                now[0] += 3.3
            errors = [r for r in caplog.records if r.levelname == "ERROR"]
            # The streak starts at t=0 and failure i ends at 5i + 1.7 s: the first beyond 30 s
            # is the 7th (31.7 s), the next one at least 30 s later the 13th (61.7 s).
            assert len(errors) == 2
            assert "consecutive_commit_failures=7" in errors[0].getMessage()
            assert "consecutive_commit_failures=13" in errors[1].getMessage()

            assert committer.commit(fake.all[14]) is True  # coordinator back
        assert "consecutive_commit_failures" not in committer.counts
        assert "offset commits recovered" in caplog.text and offsets(fake) == [14]

    def test_short_outage_logs_no_error(self, caplog: pytest.LogCaptureFixture) -> None:
        fake, committer, now, _ = self.make()
        with caplog.at_level("WARNING", logger="src.streaming.consumer"):
            for i in range(3):
                fake.commit_errors = [KafkaError.NOT_COORDINATOR] * 4
                committer.commit(fake.all[i])
                now[0] += 5.0
        assert not [r for r in caplog.records if r.levelname == "ERROR"]

    def test_partition_assignment_and_revocation_are_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake, dlq = FakeConsumer(values(1, 2)), FakeDeadLetterSink()
        fake.poll_hook = lambda c: c.revoke() if c.committed and c.assigned else None
        with caplog.at_level("INFO", logger="src.streaming.consumer"):
            run(fake, FlakyScorer(), dlq)
        assert "partitions assigned: t[0]" in caplog.text
        assert "partitions revoked: t[0]" in caplog.text

    def test_skipped_duplicates_are_logged_with_their_id(
        self, bundle: ModelBundle, caplog: pytest.LogCaptureFixture
    ) -> None:
        fake, dlq = FakeConsumer(values(3, 3)), FakeDeadLetterSink()
        with caplog.at_level("INFO", logger="src.streaming.consumer"):
            counts = run(fake, Scorer(bundle, InMemoryStore()), dlq)
        assert counts["duplicate"] == 1
        assert "duplicate id=3 skipped topic=t partition=0 offset=1" in caplog.text

    def test_duplicates_and_conflicting_ids_never_add_or_overwrite_records(
        self, bundle: ModelBundle
    ) -> None:
        store = InMemoryStore()
        original = event(TransactionID=5, TransactionAmt=50.0)
        conflicting = event(TransactionID=5, TransactionAmt=9_999.0)
        fake = FakeConsumer([encode(original)[1], encode(original)[1], encode(conflicting)[1]])
        dlq = FakeDeadLetterSink()
        counts = run(fake, Scorer(bundle, store), dlq)
        assert counts["scored"] == 1 and counts["duplicate"] == 2 and dlq.sent == []
        stored = store.get(5)
        assert stored is not None and stored.event["TransactionAmt"] == 50.0  # not overwritten
        assert len(store) == 1 and len(store.alerts) <= 1 and offsets(fake) == [0, 1, 2]


class FakeAdmin:
    """AdminClient stand-in: a new topic is unknown to describe_configs for ``lag`` calls."""

    def __init__(self, existing: dict[str, str] | None = None, lag: int = 0) -> None:
        self.retention = dict(existing or {})
        self.lag, self.describes = lag, 0
        self.created: list[NewTopic] = []

    def list_topics(self, timeout: float) -> SimpleNamespace:
        return SimpleNamespace(topics=dict.fromkeys(self.retention))

    def create_topics(self, topics: list[NewTopic]) -> dict[str, Future[None]]:
        out: dict[str, Future[None]] = {}
        for nt in topics:
            self.created.append(nt)
            self.retention[nt.topic] = nt.config["retention.ms"]
            out[nt.topic] = done(None)
        return out

    def describe_configs(
        self, resources: list[ConfigResource]
    ) -> dict[ConfigResource, Future[Any]]:
        self.describes += 1
        (res,) = resources
        if self.lag:
            self.lag -= 1
            return {res: failed(KafkaException(KafkaError(KafkaError.UNKNOWN_TOPIC_OR_PART)))}
        return {res: done({"retention.ms": SimpleNamespace(value=self.retention[res.name])})}


def done(value: Any) -> Future[Any]:
    f: Future[Any] = Future()
    f.set_result(value)
    return f


def failed(exc: BaseException) -> Future[Any]:
    f: Future[Any] = Future()
    f.set_exception(exc)
    return f


class TestEnsureDlqTopic:
    def test_creates_the_topic_with_30_day_retention(self) -> None:
        admin = FakeAdmin()
        assert ensure_dlq_topic(admin, "transactions.dlq") == "2592000000"
        (nt,) = admin.created
        assert (nt.topic, nt.num_partitions, nt.config["retention.ms"]) == (
            "transactions.dlq",
            1,
            "2592000000",
        )
        assert nt.config["cleanup.policy"] == "delete"

    def test_waits_for_a_new_topic_to_become_describable(self) -> None:
        """Regression: describe_configs right after create_topics hit UNKNOWN_TOPIC_OR_PART."""
        admin = FakeAdmin(lag=2)
        assert ensure_dlq_topic(admin, "t.dlq") == "2592000000"
        assert admin.describes == 3

    def test_existing_topic_is_reported_never_altered(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        admin = FakeAdmin(existing={"t.dlq": "604800000"})
        assert ensure_dlq_topic(admin, "t.dlq") == "604800000"
        assert admin.created == [] and "differs from the expected" in caplog.text

    def test_other_errors_are_raised(self) -> None:
        class Broken(FakeAdmin):
            def describe_configs(
                self, resources: list[ConfigResource]
            ) -> dict[ConfigResource, Future[Any]]:
                return {
                    resources[0]: failed(
                        KafkaException(KafkaError(KafkaError.TOPIC_AUTHORIZATION_FAILED))
                    )
                }

        with pytest.raises(KafkaException):
            ensure_dlq_topic(Broken(existing={"t.dlq": "1"}), "t.dlq")
