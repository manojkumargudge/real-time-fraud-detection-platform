"""Verify a synthetic Kafka cohort across a cold broker upgrade and rollback."""

import os
import sys
from hashlib import sha256

from confluent_kafka import Consumer, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic

TOPIC = "isolated-upgrade-checkpoint"
GROUP = "isolated-upgrade-checkpoint"
RECORDS = [b"fixture-A", b"fixture-B", b"fixture-C"]


def produce(bootstrap: str, records: list[bytes]) -> None:
    producer = Producer({"bootstrap.servers": bootstrap, "enable.idempotence": True})
    failures = []

    def check(error, _record):
        if error is not None:
            failures.append(str(error))

    for record in records:
        producer.produce(TOPIC, record, partition=0, on_delivery=check)
    assert producer.flush(15) == 0 and not failures, failures


def observe(bootstrap: str, count: int, prior_commit: int | None, *, commit: bool) -> None:
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": GROUP,
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
        }
    )
    try:
        partition = TopicPartition(TOPIC, 0)
        if prior_commit is not None:
            assert consumer.committed([partition], timeout=15)[0].offset == prior_commit
        assert consumer.get_watermark_offsets(partition, timeout=15) == (0, count)
        consumer.assign([TopicPartition(TOPIC, 0, 0)])
        cohort = []
        for offset in range(count):
            message = consumer.poll(15)
            assert message is not None and message.error() is None and message.offset() == offset
            cohort.append(message.value())
        assert cohort == RECORDS[:count]
        if commit:
            confirmed = consumer.commit(
                offsets=[TopicPartition(TOPIC, 0, count)], asynchronous=False
            )
            assert len(confirmed) == 1 and confirmed[0].error is None
        print(f"offset={count} payload_sha256={sha256(b','.join(cohort)).hexdigest()}")
    finally:
        consumer.close()


def main() -> None:
    mode = sys.argv[1]
    bootstrap = os.environ["TEST_KAFKA_BOOTSTRAP"]
    if mode == "seed":
        admin = AdminClient({"bootstrap.servers": bootstrap})
        admin.create_topics([NewTopic(TOPIC, 1, 1)])[TOPIC].result(15)
        produce(bootstrap, RECORDS[:2])
        observe(bootstrap, 2, None, commit=True)
    elif mode == "upgrade":
        observe(bootstrap, 2, 2, commit=False)
        produce(bootstrap, RECORDS[2:])
        observe(bootstrap, 3, 2, commit=True)
    elif mode == "rollback":
        observe(bootstrap, 3, 3, commit=False)
    else:
        raise SystemExit("expected seed, upgrade or rollback")


if __name__ == "__main__":
    main()
