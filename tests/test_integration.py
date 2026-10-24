"""Integration tests against live PostgreSQL and Kafka (``docker compose up -d kafka postgres``).

Enable with ``TEST_DATABASE_URL`` (e.g. postgresql://fraud:<pw>@localhost:5433/fraud),
``TEST_KAFKA_BOOTSTRAP`` (e.g. localhost:9092) and ``TEST_REDIS`` (e.g. localhost:16379; Feast
tests use their own project namespace and tear it down). Each test uses its own throwaway schema /
topic and consumer group, so existing data is never touched.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pandas as pd
import psycopg
import pytest
from confluent_kafka import Consumer, KafkaError, KafkaException, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic
from psycopg.conninfo import make_conninfo
from pydantic import ValidationError

from src.serving.config import Settings
from src.serving.model import ModelBundle
from src.serving.schema import ScoreResponse, TransactionEvent
from src.serving.scorer import Scorer
from src.serving.store import InMemoryStore, PostgresStore, ScoredRecord
from src.streaming import consumer as consumer_mod
from src.streaming.events import SCHEMA_HEADER, encode
from src.streaming.producer import publish
from src.streaming.reliability import (
    DLQ_RETENTION_MS,
    DeadLetter,
    KafkaDeadLetterSink,
    RetryPolicy,
    build_dlq_producer,
    ensure_dlq_topic,
)
from tests.test_serving import event

DB_URL = os.environ.get("TEST_DATABASE_URL")
KAFKA = os.environ.get("TEST_KAFKA_BOOTSTRAP")
REDIS = os.environ.get("TEST_REDIS")

pytestmark = pytest.mark.integration
needs_db = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")
needs_kafka = pytest.mark.skipif(not KAFKA, reason="TEST_KAFKA_BOOTSTRAP not set")
needs_redis = pytest.mark.skipif(not REDIS, reason="TEST_REDIS not set")


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
        stored = pg_store.get(1)
        assert stored is not None and stored.transaction_dt == 100
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
@pytest.mark.parametrize(
    ("overrides", "db_error"),
    [
        ({"ProductCD": "W\x00"}, psycopg.errors.UntranslatableCharacter),
        ({"TransactionID": 2**63}, psycopg.errors.NumericValueOutOfRange),
    ],
)
def test_values_postgres_cannot_store_are_rejected_by_the_schema(
    pg_store: PostgresStore, overrides: dict[str, Any], db_error: type[Exception]
) -> None:
    """Why the schema limits exist: PostgreSQL rejects these on every attempt."""
    ev = {**event(), **overrides}
    with pytest.raises(db_error):
        pg_store.save(replace(record(1, 100), transaction_id=ev["TransactionID"], event=ev))
    assert pg_store.ping() and pg_store.save(record(2, 100))  # store still usable
    with pytest.raises(ValidationError):
        TransactionEvent.model_validate(ev)


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


@dataclass
class KafkaEnv:
    """Throwaway source + DLQ topics and consumer group on the live broker."""

    admin: Any
    settings: Settings
    retention: str  # DLQ retention.ms as provisioned by ensure_dlq_topic
    partitions: int = 2

    @property
    def topic(self) -> str:
        return self.settings.kafka_topic

    @property
    def dlq(self) -> str:
        return self.settings.kafka_dlq_topic

    def sink(self) -> KafkaDeadLetterSink:
        assert KAFKA
        return KafkaDeadLetterSink(build_dlq_producer(KAFKA), self.dlq)

    def consumer(self) -> Any:
        return consumer_mod.build_consumer(self.settings)

    def read_dlq(self, expected: int, timeout: float = 15.0) -> list[Any]:
        """All DLQ records: wait for ``expected``, then 2 s more to catch any extras."""
        c = Consumer(
            {
                "bootstrap.servers": KAFKA,
                "group.id": f"probe-{uuid.uuid4().hex[:8]}",
                "auto.offset.reset": "earliest",
                "enable.auto.commit": False,
            }
        )
        c.subscribe([self.dlq])
        out: list[Any] = []
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                msg = c.poll(0.5)
                if msg is not None and not msg.error():
                    out.append(msg)
                if len(out) >= expected:
                    deadline = min(deadline, time.monotonic() + 2.0)
        finally:
            c.close()
        return out

    def offsets(self) -> list[tuple[int, int]]:
        """(committed, end) per source partition; committed < 0 means nothing committed."""
        tps = [TopicPartition(self.topic, p) for p in range(self.partitions)]
        c = Consumer({"bootstrap.servers": KAFKA, "group.id": self.settings.kafka_group_id})
        try:
            committed = [tp.offset for tp in c.committed(tps, timeout=10)]
            ends = [c.get_watermark_offsets(tp, timeout=10)[1] for tp in tps]
        finally:
            c.close()
        return list(zip(committed, ends, strict=True))

    def fully_committed(self) -> bool:
        return all(c == e or (e == 0 and c < 0) for c, e in self.offsets())


@pytest.fixture
def kafka_env() -> Iterator[KafkaEnv]:
    assert KAFKA
    tag = uuid.uuid4().hex[:8]
    topic, dlq = f"test-src-{tag}", f"test-dlq-{tag}"
    admin = AdminClient({"bootstrap.servers": KAFKA})
    admin.create_topics([NewTopic(topic, num_partitions=2, replication_factor=1)])[topic].result(30)
    try:
        retention = ensure_dlq_topic(admin, dlq)
        settings = replace(
            Settings.from_env(),
            kafka_bootstrap_servers=KAFKA,
            kafka_topic=topic,
            kafka_group_id=f"test-{tag}",
            kafka_dlq_topic=dlq,
        )
        yield KafkaEnv(admin, settings, retention)
    finally:
        # Delete unconditionally: a just-created topic can be missing from list_topics()
        # for a moment, which leaked empty DLQ topics when cleanup filtered on it.
        for t in (topic, dlq):
            try:
                admin.delete_topics([t])[t].result(30)
            except KafkaException as exc:  # never created (setup failed first)
                if exc.args[0].code() != KafkaError.UNKNOWN_TOPIC_OR_PART:
                    raise


def produce(topic: str, values: list[bytes], keys: list[bytes | None] | None = None) -> None:
    assert KAFKA
    producer = Producer({"bootstrap.servers": KAFKA, "acks": "all"})
    for i, v in enumerate(values):
        producer.produce(topic, key=None if keys is None else keys[i], value=v)
    assert producer.flush(10) == 0


@dataclass
class FailingFor(Scorer):
    """A scorer whose model fails deterministically for one transaction."""

    fail_id: int = 0

    def score_event(self, event: TransactionEvent) -> ScoreResponse:
        if event.TransactionID == self.fail_id:
            raise RuntimeError("model failure for this event")
        return super().score_event(event)


class OutageStore:
    """Delegates to a real store, but the database is 'down' for its first lookups."""

    name = "postgres-with-outage"

    def __init__(self, inner: PostgresStore, failures: int) -> None:
        self.inner, self.failures = inner, failures

    def get(self, transaction_id: int) -> ScoredRecord | None:
        if self.failures:
            self.failures -= 1
            raise psycopg.OperationalError("connection to server failed: Connection refused")
        return self.inner.get(transaction_id)

    def card_history(self, card_id: str, before_dt: int) -> list[dict[str, Any]]:
        return self.inner.card_history(card_id, before_dt)

    def save(self, record: ScoredRecord) -> bool:
        return self.inner.save(record)

    def ping(self) -> bool:
        return self.inner.ping()

    def close(self) -> None:
        self.inner.close()


QUICK = RetryPolicy(base_delay=0.05, max_delay=0.2)


@needs_kafka
def test_kafka_round_trip_with_duplicate_and_poison(bundle_dir: Path, kafka_env: KafkaEnv) -> None:
    topic = kafka_env.topic
    events = [event(TransactionID=i, TransactionDT=100_000 + 60 * i) for i in range(1, 6)]
    producer = Producer({"bootstrap.servers": KAFKA, "acks": "all"})
    assert publish(producer, topic, [*events, events[0]]) == 6  # one duplicate
    producer.produce(topic, value=b"{not json", headers=[SCHEMA_HEADER])
    producer.produce(topic, value=json.dumps(event(TransactionID=99, isFraud=1)).encode())
    producer.flush(10)

    store = InMemoryStore()
    scorer = Scorer(ModelBundle.load(bundle_dir), store)
    counts = consumer_mod.run(
        kafka_env.consumer(),
        scorer,
        topic,
        dead_letters=kafka_env.sink(),
        max_messages=8,
        idle_timeout=20,
    )
    assert counts["scored"] == 5 and counts["duplicate"] == 1 and counts["invalid"] == 2
    assert sorted(store._records) == [1, 2, 3, 4, 5]
    assert len(kafka_env.read_dlq(expected=2)) == 2  # the two invalid messages, nothing else

    # Offsets were committed: a new consumer in the same group sees nothing new.
    again = consumer_mod.run(
        kafka_env.consumer(), scorer, topic, dead_letters=kafka_env.sink(), idle_timeout=5
    )
    assert sum(again.values()) == 0 and kafka_env.fully_committed()
    assert encode(events[0])[0].decode().startswith("1111|")


@needs_kafka
@needs_db
def test_consumer_survives_former_poison_messages(
    bundle_dir: Path, pg_store: PostgresStore, kafka_env: KafkaEnv
) -> None:
    """Regression: these events used to crash the consumer before commit, on every restart."""
    poison = [
        event(TransactionID=2**53 + 1),  # lost precision in the float64 feature path
        event(TransactionID=2**63),  # beyond BIGINT
        event(TransactionID=3, card1=10**400),  # float overflow
        event(TransactionID=4, ProductCD="W\x00"),  # JSONB cannot store NUL
    ]
    good = [event(TransactionID=i, TransactionDT=100_000 + 60 * i) for i in (1, 2)]
    # Poison first, then valid events: the consumer must get past the poison to score them.
    produce(kafka_env.topic, [json.dumps(e).encode() for e in poison])
    produce(kafka_env.topic, [encode(e)[1] for e in good], [encode(e)[0] for e in good])

    scorer = Scorer(ModelBundle.load(bundle_dir), pg_store)
    counts = consumer_mod.run(
        kafka_env.consumer(),
        scorer,
        kafka_env.topic,
        dead_letters=kafka_env.sink(),
        max_messages=6,
        idle_timeout=20,
    )
    assert counts["invalid"] == 4 and counts["scored"] == 2
    assert pg_store.counts()["scored_transactions"] == 2
    assert len(kafka_env.read_dlq(expected=4)) == 4
    again = consumer_mod.run(
        kafka_env.consumer(), scorer, kafka_env.topic, dead_letters=kafka_env.sink(), idle_timeout=5
    )
    assert sum(again.values()) == 0 and kafka_env.fully_committed()  # poison included


@needs_kafka
def test_dlq_topic_is_provisioned_with_30_day_retention(kafka_env: KafkaEnv) -> None:
    assert kafka_env.retention == str(DLQ_RETENTION_MS) == "2592000000"
    assert ensure_dlq_topic(kafka_env.admin, kafka_env.dlq) == "2592000000"  # idempotent
    assert len(kafka_env.admin.list_topics(timeout=10).topics[kafka_env.dlq].partitions) == 1


@needs_kafka
@needs_db
def test_failed_and_invalid_events_reach_the_dlq_with_metadata(
    bundle_dir: Path, pg_store: PostgresStore, kafka_env: KafkaEnv
) -> None:
    evs = [event(TransactionID=i, TransactionDT=100_000 + 60 * i) for i in (1, 2, 3)]
    raw = [encode(e)[1] for e in evs]
    # One key, so all four land on one partition at offsets 0..3.
    produce(kafka_env.topic, [raw[0], raw[1], b'{"TransactionID": 9, oops', raw[2]], [b"k"] * 4)

    scorer = FailingFor(ModelBundle.load(bundle_dir), pg_store, fail_id=2)
    counts = consumer_mod.run(
        kafka_env.consumer(),
        scorer,
        kafka_env.topic,
        dead_letters=kafka_env.sink(),
        policy=QUICK,
        max_messages=4,
        idle_timeout=20,
    )
    assert counts["scored"] == 2 and counts["failed"] == 1 and counts["invalid"] == 1
    assert counts["retries"] == 3 and counts["dead_lettered"] == 2
    assert pg_store.counts()["scored_transactions"] == 2  # events 1 and 3; nothing skipped

    letters = {dict(m.headers())["dlq.reason"]: m for m in kafka_env.read_dlq(expected=2)}
    failed, invalid = letters[b"processing_error"], letters[b"invalid"]
    fh, ih = dict(failed.headers()), dict(invalid.headers())
    assert failed.value() == raw[1] and failed.key() == b"k"  # the full original event
    assert (fh["dlq.attempts"], fh["dlq.source.topic"]) == (b"4", kafka_env.topic.encode())
    assert fh["dlq.source.offset"] == b"1" and fh["dlq.error.type"] == b"RuntimeError"
    assert (ih["dlq.attempts"], ih["dlq.source.offset"]) == (b"1", b"2")
    assert ih["dlq.error.detail"].startswith(b"json:")
    assert kafka_env.fully_committed()


@needs_kafka
@needs_db
def test_database_outage_is_retried_without_dlq_or_skipped_events(
    bundle_dir: Path, pg_store: PostgresStore, kafka_env: KafkaEnv
) -> None:
    evs = [event(TransactionID=i, TransactionDT=100_000 + 60 * i) for i in (1, 2, 3)]
    produce(kafka_env.topic, [encode(e)[1] for e in evs], [b"k"] * 3)
    store = OutageStore(pg_store, failures=8)  # longer than the 3-retry limit
    counts = consumer_mod.run(
        kafka_env.consumer(),
        Scorer(ModelBundle.load(bundle_dir), store),
        kafka_env.topic,
        dead_letters=kafka_env.sink(),
        policy=QUICK,
        max_messages=3,
        idle_timeout=20,
    )
    assert counts["scored"] == 3 and counts["retries"] == 8 and counts["dead_lettered"] == 0
    assert pg_store.counts()["scored_transactions"] == 3
    assert kafka_env.read_dlq(expected=0, timeout=4) == [] and kafka_env.fully_committed()


@needs_kafka
def test_unreachable_dlq_blocks_the_commit_and_a_restart_recovers(
    bundle_dir: Path, kafka_env: KafkaEnv
) -> None:
    produce(kafka_env.topic, [b"{poison"], [b"k"])
    stop = threading.Event()
    dead_broker = Producer({"bootstrap.servers": "127.0.0.1:1", "message.timeout.ms": 1000})
    inner = KafkaDeadLetterSink(dead_broker, kafka_env.dlq, timeout=1.5)

    class StopAfterTwo:
        attempts = 0

        def send(self, letter: DeadLetter) -> None:
            self.attempts += 1
            if self.attempts == 2:
                stop.set()  # shutdown while the DLQ is unreachable
            inner.send(letter)

    sink = StopAfterTwo()
    scorer = Scorer(ModelBundle.load(bundle_dir), InMemoryStore())
    counts = consumer_mod.run(
        kafka_env.consumer(),
        scorer,
        kafka_env.topic,
        dead_letters=sink,
        policy=QUICK,
        idle_timeout=20,
        stop=stop,
    )
    assert sink.attempts == 2 and counts["interrupted"] == 1 and counts["dead_lettered"] == 0
    assert not kafka_env.fully_committed()  # the poison offset was NOT committed
    assert kafka_env.read_dlq(expected=0, timeout=3) == []

    # Restart with the DLQ reachable: the message is redelivered and dead-lettered once.
    counts = consumer_mod.run(
        kafka_env.consumer(),
        scorer,
        kafka_env.topic,
        dead_letters=kafka_env.sink(),
        idle_timeout=10,
    )
    assert counts["invalid"] == 1 and counts["dead_lettered"] == 1
    assert len(kafka_env.read_dlq(expected=1)) == 1 and kafka_env.fully_committed()


@needs_redis
def test_feast_redis_replay_matches_the_store_path(bundle_dir: Path, tmp_path: Path) -> None:
    """Same replay as the SQLite unit test, on Redis: catches Redis-only write semantics (it
    drops writes whose timestamp is not newer, which would lose same-timestamp updates)."""
    from src.features import feast_store as fs
    from tests.test_entity_state import arrival_order, make_events

    assert REDIS
    bundle = ModelBundle.load(bundle_dir)
    project = f"test_{uuid.uuid4().hex[:8]}"
    store = fs.open_store(tmp_path, redis=REDIS, config=bundle.history_config, project=project)
    try:
        feast = fs.FeastFeatures(store, bundle.history_config)
        by_store = Scorer(bundle, InMemoryStore())
        by_feast = Scorer(bundle, InMemoryStore(), feast=feast, read_from_feast=True)
        events = arrival_order(make_events(120, 9), 9)
        ties = pd.DataFrame(events).duplicated(["card1", "addr1", "TransactionDT"]).sum()
        assert ties > 0  # same-timestamp events on one uid are part of the replay
        for ev in events:
            a = by_store.score_event(TransactionEvent.model_validate(ev))
            b = by_feast.score_event(TransactionEvent.model_validate(ev))
            for name, va in a.history_features.items():
                vb = b.history_features[name]
                assert (va is None) == (vb is None), (ev["TransactionID"], name)
                if va is not None and vb is not None:
                    assert vb == pytest.approx(va, rel=1e-6), (ev["TransactionID"], name)
            assert b.score == pytest.approx(a.score, abs=1e-6)
    finally:
        store.teardown()  # deletes this project's keys from Redis


@needs_db
@pytest.mark.parametrize("mode", ["fired", "forced"])
def test_drift_job_end_to_end_writes_drift_reports(
    mode: str,
    synthetic_frame: pd.DataFrame,
    pg_store: PostgresStore,
    pg_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drift job on the test stack: scored rows in Postgres, a throwaway MLflow registry with
    champion = production = v1, retraining enabled. Checks the drift_reports row and that
    production never moves."""
    pytest.importorskip("mlflow")
    from mlflow.tracking import MlflowClient

    from src.data.load import KEY
    from src.features.history import TIME, HistoryConfig
    from src.models.compare import build_model_frame
    from src.models.tracking import log_training_run
    from src.models.train import fit_baseline, save_bundle
    from src.monitoring import run_drift as job
    from tests.test_models import FAST
    from tests.test_monitoring import quiet_cfg, small_cfg
    from tests.test_tracking import _repo_state

    before = _repo_state()  # the repo's own mlflow.db / mlruns must stay untouched
    uri = f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "sqlite:///unused-default-must-not-be-hit.db")
    run = fit_baseline(synthetic_frame, FAST)
    bundle_dir = save_bundle(run.trained, run.metadata, tmp_path / "champion")
    log_training_run(run, bundle_dir, imbalance_method="none", register=True, uri=uri)
    client = MlflowClient(tracking_uri=uri, registry_uri=uri)
    client.set_registered_model_alias("fraud-xgboost", "production", "1")

    champion, source = job._load_champion(True, bundle_dir, uri)
    frame = build_model_frame(synthetic_frame, "baseline", HistoryConfig())
    after = frame.loc[frame[TIME] >= int(champion.metadata["split"]["train_end"])]
    scores = champion.predict(after)
    for tid, dt, s in zip(after[KEY], after[TIME], scores, strict=True):
        pg_store.save(
            ScoredRecord(
                transaction_id=int(tid),
                transaction_dt=int(dt),
                card_id="c",
                uid="u",
                event={"TransactionID": int(tid)},
                score=float(s),
                threshold=champion.threshold,
                is_alert=bool(s >= champion.threshold),
                model_version=champion.model_version,
            )
        )

    from src.monitoring.report_store import load_scored

    # "fired": a share threshold low on purpose; "forced": nothing can fire, --force-retrain
    # runs the retrain path anyway.
    fired_cfg = small_cfg(feature_drift={"share_threshold": 0.01})
    cfg = fired_cfg if mode == "fired" else quiet_cfg()
    report_id = job.run_job(
        job.JobInputs(raw=synthetic_frame, scored=load_scored(pg_dsn), champion=champion),
        cfg,
        job.JobOptions(
            reports_dir=tmp_path / "reports",
            mlflow_uri=uri,
            champion_source=source,
            force_retrain=mode == "forced",
        ),
        pg_dsn,
    )

    with psycopg.connect(pg_dsn) as conn:
        row = conn.execute(
            "SELECT triggered, retrain_run_id, challenger_version, promotion, windows, "
            "report_path, synthetic, current_rows, forced, top_gain_features "
            "FROM drift_reports WHERE id = %s",
            (report_id,),
        ).fetchone()
    assert row is not None
    (
        triggered,
        run_id,
        version,
        promotion,
        windows,
        report_path,
        synthetic,
        current_rows,
        forced,
        top_gain,
    ) = row
    assert triggered == (mode == "fired") and forced == (mode == "forced")
    assert run_id and version == "2" and not synthetic and current_rows > 0
    assert len(top_gain) == 5
    assert {"challenger_pr_auc", "champion_pr_auc", "ci95_low", "ci95_high"} <= set(promotion)
    assert promotion["bootstrap"]["resamples"] > 0
    assert windows["holdout"]["end_dt"] == windows["label_cutoff"]["dt"]
    assert windows["challenger_train"]["end_dt"] < windows["holdout"]["start_dt"]
    assert Path(report_path).is_file()
    assert str(client.get_model_version_by_alias("fraud-xgboost", "challenger").version) == "2"
    assert str(client.get_model_version_by_alias("fraud-xgboost", "production").version) == "1"
    expected_champion = "2" if promotion["promote"] else "1"
    assert str(client.get_model_version_by_alias("fraud-xgboost", "champion").version) == (
        expected_champion
    )
    assert _repo_state() == before
    assert not Path("unused-default-must-not-be-hit.db").exists()
