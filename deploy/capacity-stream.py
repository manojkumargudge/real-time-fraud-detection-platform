"""Publish or audit the fixed synthetic Kafka cohort in an isolated capacity stack."""

import json
import os
import sys
import time

import psycopg
from confluent_kafka import Consumer, Producer, TopicPartition

from src.streaming.events import SCHEMA_HEADER, encode

broker = "kafka:19092"
topic = "transactions"
first = 200_000_000
if os.environ.get("FRAUD_CAPACITY_FIXTURE") != "1":
    raise SystemExit("set FRAUD_CAPACITY_FIXTURE=1 only in an isolated synthetic stack")
if len(sys.argv) != 3 or sys.argv[1] not in {"produce", "audit", "audit-main"}:
    raise SystemExit("usage: python - {produce|audit|audit-main} COUNT")
count = int(sys.argv[2])
if count <= 0:
    raise SystemExit("COUNT must be positive")

if sys.argv[1] == "produce":
    producer = Producer({"bootstrap.servers": broker, "enable.idempotence": True})
    start = time.monotonic()
    errors = []

    def confirm(error, _message):
        if error is not None:
            errors.append(str(error))

    for index in range(count):
        key, value = encode(
            {
                "TransactionID": first + index,
                "TransactionDT": 199_000,
                "TransactionAmt": 5 + index % 97,
                "ProductCD": "W",
                "card1": 20_000 + index % 256,
                "card4": "visa",
                "card6": "debit",
                "addr1": 315,
            }
        )
        producer.produce(topic, key=key, value=value, headers=[SCHEMA_HEADER], on_delivery=confirm)
        producer.poll(0)
        time.sleep(max(0, start + (index + 1) / 10 - time.monotonic()))
    assert producer.flush(30) == 0 and not errors, errors[:3]
    print(
        json.dumps(
            {"produced": count, "target_rate": 10, "elapsed_seconds": time.monotonic() - start}
        )
    )
else:
    reader = Consumer(
        {"bootstrap.servers": broker, "group.id": "fraud-scorer", "enable.auto.commit": False}
    )
    partitions = [TopicPartition(topic, p) for p in range(3)]
    try:
        offsets = [p.offset for p in reader.committed(partitions, timeout=8)]
        ends = [reader.get_watermark_offsets(p, timeout=8)[1] for p in partitions]
        dlq = reader.get_watermark_offsets(TopicPartition("transactions.dlq", 0), timeout=8)[1]
    finally:
        reader.close()
    with psycopg.connect(
        os.environ["DATABASE_URL"], options="-c default_transaction_read_only=on"
    ) as pg:
        total, minimum, maximum = pg.execute(
            "SELECT count(*), min(transaction_id), max(transaction_id) "
            "FROM scored_transactions WHERE transaction_id >= %s",
            (first,),
        ).fetchone()
        journal = (
            pg.execute("SELECT count(*) FROM pending_feast_updates").fetchone()[0]
            if sys.argv[1] == "audit"
            else None
        )
        digest = pg.execute(
            "SELECT md5(string_agg(transaction_id::text || ':' || score::text || ':' "
            "|| is_alert::text, ',' ORDER BY transaction_id)) "
            "FROM scored_transactions WHERE transaction_id >= %s",
            (first,),
        ).fetchone()[0]
    assert sum(ends) == count and dlq == 0
    assert all(
        end == offset or (end == 0 and offset < 0)
        for offset, end in zip(offsets, ends, strict=True)
    ), offsets
    assert (
        total == count
        and minimum == first
        and maximum == first + count - 1
        and journal in (None, 0)
    )
    print(
        json.dumps(
            {
                "kafka_scores": total,
                "min_id": minimum,
                "max_id": maximum,
                "ends": ends,
                "commits": offsets,
                "dlq": dlq,
                "pending_feast": journal,
                "score_alert_digest": digest,
            }
        )
    )
