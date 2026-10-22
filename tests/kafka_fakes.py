"""In-memory stand-ins for the confluent-kafka objects the consumer uses (no broker needed)."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from confluent_kafka import KafkaError, KafkaException, TopicPartition

from src.streaming.reliability import DeadLetter, DeadLetterError, RetryPolicy

NO_WAIT = RetryPolicy(base_delay=0.0, max_delay=0.0)


class FakeError:
    def __init__(self, code: int) -> None:
        self._code = code

    def code(self) -> int:
        return self._code


class FakeMessage:
    def __init__(
        self,
        value: bytes | None,
        error: FakeError | None = None,
        *,
        topic: str = "t",
        partition: int = 0,
        offset: int = 0,
        key: bytes | None = None,
        headers: list[tuple[str, bytes]] | None = None,
    ) -> None:
        self._value, self._error, self._key, self._headers = value, error, key, headers
        self._topic, self._partition, self._offset = topic, partition, offset

    def value(self) -> bytes | None:
        return self._value

    def error(self) -> FakeError | None:
        return self._error

    def topic(self) -> str:
        return self._topic

    def partition(self) -> int:
        return self._partition

    def offset(self) -> int:
        return self._offset

    def key(self) -> bytes | None:
        return self._key

    def headers(self) -> list[tuple[str, bytes]] | None:
        return self._headers

    def timestamp(self) -> tuple[int, int]:
        return 1, 1_700_000_000_000 + self._offset


class FakeConsumer:
    """One partition, offsets 0..n-1. Paused partitions deliver nothing unless
    ``ignore_pause`` (to simulate already-fetched messages slipping through); ``seek``
    re-queues the message so it is delivered again."""

    def __init__(self, values: Sequence[bytes | None], *, topic: str = "t") -> None:
        self.all = [FakeMessage(v, topic=topic, offset=i) for i, v in enumerate(values)]
        self.queue = list(self.all)
        self.assigned = {(topic, 0)}
        self.paused: set[tuple[str, int]] = set()
        self.committed: list[FakeMessage] = []
        self.seeks: list[tuple[str, int, int]] = []
        self.pause_calls = 0
        self.closed = False
        self.ignore_pause = False
        self.commit_errors: list[int] = []  # KafkaError codes raised by the next commits
        self.poll_hook: Callable[[FakeConsumer], None] | None = None
        self.on_assign: Callable[..., None] | None = None
        self.on_revoke: Callable[..., None] | None = None

    def subscribe(
        self,
        topics: list[str],
        on_assign: Callable[..., None] | None = None,
        on_revoke: Callable[..., None] | None = None,
    ) -> None:
        self.topics, self.on_assign, self.on_revoke = topics, on_assign, on_revoke

    def assignment(self) -> list[TopicPartition]:
        return [TopicPartition(t, p) for t, p in sorted(self.assigned)]

    def pause(self, partitions: list[TopicPartition]) -> None:
        self.pause_calls += 1
        self.paused.update((p.topic, p.partition) for p in partitions)

    def resume(self, partitions: list[TopicPartition]) -> None:
        self.paused.difference_update((p.topic, p.partition) for p in partitions)

    def seek(self, tp: TopicPartition) -> None:
        self.seeks.append((tp.topic, tp.partition, tp.offset))
        self.queue.insert(0, self.all[tp.offset])

    def revoke(self) -> None:
        """Simulate a rebalance taking this consumer's partition away."""
        revoked = self.assignment()
        self.assigned.clear()
        if self.on_revoke is not None:
            self.on_revoke(self, revoked)

    def poll(self, _timeout: float) -> FakeMessage | None:
        if self.on_assign is not None:  # like librdkafka: the first poll joins the group
            on_assign, self.on_assign = self.on_assign, None
            on_assign(self, self.assignment())
        if self.poll_hook is not None:
            self.poll_hook(self)
        for i, m in enumerate(self.queue):
            if (
                m.error() is not None
                or self.ignore_pause
                or (m.topic(), m.partition()) not in self.paused
            ):
                return self.queue.pop(i)
        return None

    def commit(self, message: FakeMessage, asynchronous: bool) -> None:
        assert asynchronous is False
        if self.commit_errors:
            raise KafkaException(KafkaError(self.commit_errors.pop(0)))
        self.committed.append(message)

    def close(self) -> None:
        self.closed = True


class FakeDeadLetterSink:
    """Confirms every write, after ``failures`` unconfirmed attempts."""

    def __init__(self, failures: int = 0, on_attempt: Callable[[int], None] | None = None) -> None:
        self.failures, self.on_attempt = failures, on_attempt
        self.attempts = 0
        self.sent: list[DeadLetter] = []

    def send(self, letter: DeadLetter) -> None:
        self.attempts += 1
        if self.on_attempt is not None:
            self.on_attempt(self.attempts)
        if self.failures:
            self.failures -= 1
            raise DeadLetterError("broker unavailable")
        self.sent.append(letter)
