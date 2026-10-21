"""Integration tests against live PostgreSQL and Kafka (``docker compose up -d kafka postgres``).

Enable with ``TEST_DATABASE_URL`` (e.g. postgresql://fraud:<pw>@localhost:5433/fraud) and
``TEST_KAFKA_BOOTSTRAP`` (e.g. localhost:9092). Each test uses its own throwaway schema /
topic and consumer group, so existing data is never touched.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from src.serving.config import Settings
from src.serving.model import ModelBundle
from src.serving.schema import TransactionEvent
from src.serving.scorer import Scorer
from src.serving.store import InMemoryStore, PostgresStore, ScoredRecord
from tests.test_serving import event

DB_URL = os.environ.get("TEST_DATABASE_URL")
KAFKA = os.environ.get("TEST_KAFKA_BOOTSTRAP")

pytestmark = pytest.mark.integration
needs_db = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")
needs_kafka = pytest.mark.skipif(not KAFKA, reason="TEST_KAFKA_BOOTSTRAP not set")


@pytest.fixture
def pg_dsn() -> Iterator[str]:
    assert DB_URL
    schema = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DB_URL, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
    try:
        yield make_conninfo(DB_URL, options=f"-c search_path={schema}")
    finally:
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture
def pg_store(pg_dsn: str) -> Iterator[PostgresStore]:
    store = PostgresStore(pg_dsn)
    yield store
    store.close()


def record(tid: int, dt: int, card: str = "c1", alert: bool = False) -> ScoredRecord:
    return ScoredRecord(
        transaction_id=tid,
        transaction_dt=dt,
        card_id=card,
        uid=f"{card}|u",
        event={"TransactionID": tid, "TransactionDT": dt, "TransactionAmt": 1.5},
        score=0.9 if alert else 0.1,
        threshold=0.5,
        is_alert=alert,
        model_version="test",
    )


@needs_db
class TestPostgresStore:
    def test_round_trip(self, pg_store: PostgresStore) -> None:
        r = record(1, 100)
        assert pg_store.save(r) is True
        assert pg_store.get(1) == r
        assert pg_store.get(2) is None and pg_store.ping()

    def test_duplicate_insert_is_noop(self, pg_store: PostgresStore) -> None:
        assert pg_store.save(record(1, 100, alert=True)) is True
        assert pg_store.save(record(1, 999, alert=True)) is False
        assert pg_store.get(1).transaction_dt == 100
        assert pg_store.counts() == {"scored_transactions": 1, "alerts": 1}

    def test_alert_only_for_alerting_scores(self, pg_store: PostgresStore) -> None:
        pg_store.save(record(1, 100, alert=False))
        pg_store.save(record(2, 101, alert=True))
        assert pg_store.counts() == {"scored_transactions": 2, "alerts": 1}

    def test_card_history_is_strictly_earlier_and_per_card(self, pg_store: PostgresStore) -> None:
        for tid, dt, card in [(1, 100, "a"), (2, 200, "a"), (3, 300, "a"), (4, 150, "b")]:
            pg_store.save(record(tid, dt, card))
        hist = pg_store.card_history("a", 200)
        assert [h["TransactionID"] for h in hist] == [1]

    def test_concurrent_duplicate_saves_insert_once(self, pg_dsn: str) -> None:
        stores = [PostgresStore(pg_dsn) for _ in range(4)]
        results: list[bool] = []
        barrier = threading.Barrier(len(stores))

        def save(s: PostgresStore) -> None:
            barrier.wait()
            results.append(s.save(record(7, 100, alert=True)))

        threads = [threading.Thread(target=save, args=(s,)) for s in stores]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results) == [False, False, False, True]
        assert stores[0].counts() == {"scored_transactions": 1, "alerts": 1}
        for s in stores:
            s.close()


@needs_db
def test_scorer_on_postgres_matches_in_memory(bundle_dir: Path, pg_store: PostgresStore) -> None:
    bundle = ModelBundle.load(bundle_dir)
    events = [
        event(TransactionID=i, TransactionDT=100_000 + 3_600 * i, TransactionAmt=10.0 * i)
        for i in range(1, 8)
    ]
    pg, mem = Scorer(bundle, pg_store), Scorer(bundle, InMemoryStore())
    for e in events:
        ev = TransactionEvent.model_validate(e)
        a, b = pg.score_event(ev), mem.score_event(ev)
        assert a.score == pytest.approx(b.score, rel=1e-9)
        assert a.history_features == b.history_features
    assert pg.score_event(TransactionEvent.model_validate(events[0])).duplicate


@needs_kafka
def test_kafka_round_trip_with_duplicate_and_poison(bundle_dir: Path) -> None:
    from confluent_kafka import Producer
    from confluent_kafka.admin import AdminClient, NewTopic

    from src.streaming import consumer as consumer_mod
    from src.streaming.events import SCHEMA_HEADER, encode
    from src.streaming.producer import publish

    assert KAFKA
    topic = f"test-transactions-{uuid.uuid4().hex[:8]}"
    admin = AdminClient({"bootstrap.servers": KAFKA})
    admin.create_topics([NewTopic(topic, num_partitions=2, replication_factor=1)])[topic].result(30)
    try:
        events = [event(TransactionID=i, TransactionDT=100_000 + 60 * i) for i in range(1, 6)]
        producer = Producer({"bootstrap.servers": KAFKA, "acks": "all"})
        assert publish(producer, topic, [*events, events[0]]) == 6  # one duplicate
        producer.produce(topic, value=b"{not json", headers=[SCHEMA_HEADER])
        producer.produce(topic, value=json.dumps(event(TransactionID=99, isFraud=1)).encode())
        producer.flush(10)

        settings = replace(
            Settings.from_env(),
            kafka_bootstrap_servers=KAFKA,
            kafka_topic=topic,
            kafka_group_id=f"test-{uuid.uuid4().hex[:8]}",
        )
        store = InMemoryStore()
        scorer = Scorer(ModelBundle.load(bundle_dir), store)
        counts = consumer_mod.run(
            consumer_mod.build_consumer(settings), scorer, topic, max_messages=8, idle_timeout=20
        )
        assert counts["scored"] == 5 and counts["duplicate"] == 1 and counts["invalid"] == 2
        assert sorted(store._records) == [1, 2, 3, 4, 5]

        # Offsets were committed: a new consumer in the same group sees nothing new.
        again = consumer_mod.run(
            consumer_mod.build_consumer(settings), scorer, topic, idle_timeout=5
        )
        assert sum(again.values()) == 0
        assert encode(events[0])[0].decode().startswith("1111|")
    finally:
        admin.delete_topics([topic])[topic].result(30)
