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


# --------------------------------------------------------------------------- dashboard data access
# Schema setup (src/serving/store.py) and the analyst dashboard's SQL (src/dashboard/data.py).


def _alert_ids(conn: psycopg.Connection[Any]) -> dict[int, int]:
    return dict(conn.execute("SELECT transaction_id, alert_id FROM alerts").fetchall())


def _reviews(conn: psycopg.Connection[Any]) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT id, alert_id, from_status, to_status, analyst, alert_version FROM case_reviews "
        "ORDER BY id"
    ).fetchall()


def _status(conn: psycopg.Connection[Any], alert_id: int) -> tuple[Any, ...] | None:
    return conn.execute(
        "SELECT status, version FROM alerts WHERE alert_id = %s", (alert_id,)
    ).fetchone()


def _assert_same_rows(before: list[Any], after: list[Any]) -> None:
    print(f"case_reviews: before {len(before)} rows, after {len(after)} rows")
    assert before, "the 'before' side is empty: nothing to compare"
    assert after, "the 'after' side is empty: nothing to compare"
    assert after == before


@pytest.fixture
def dash(pg_dsn: str, pg_store: PostgresStore) -> Iterator[psycopg.Connection[Any]]:
    """30 alerts (scores 0.50..0.79, transaction days 100..129) and 10 non-alerts, plus the
    ring and drift tables (normally created by their batch jobs)."""
    from src.graph.rings import TABLE_DDL
    from src.monitoring.report_store import DDL as DRIFT_DDL

    for i in range(30):
        tid = 9_000 + i
        rec = replace(record(tid, (100 + i) * 86_400 + 5, card=f"card{i % 3}", alert=True),
                      uid=f"card{i % 3}|addr", score=0.50 + i / 100)  # fmt: skip
        assert pg_store.save(rec)
    for i in range(10):
        assert pg_store.save(record(9_500 + i, 50 * 86_400 + i))
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(TABLE_DDL)
        conn.execute(DRIFT_DDL)
        yield conn


@needs_db
class TestSchemaSetup:
    def test_concurrent_setup_from_many_threads(self, pg_dsn: str) -> None:
        barrier, errors = threading.Barrier(6), []

        def setup() -> None:
            try:
                barrier.wait(timeout=30)
                PostgresStore(pg_dsn).close()
            except Exception as exc:  # collected and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=setup) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert errors == []
        with psycopg.connect(pg_dsn) as conn:
            oid = conn.execute(
                "SELECT oid FROM pg_constraint WHERE conname = 'alerts_status_check' "
                "AND conrelid = 'alerts'::regclass"
            ).fetchone()
            PostgresStore(pg_dsn).close()  # rerun: idempotent, constraint not recreated
            again = conn.execute(
                "SELECT oid FROM pg_constraint WHERE conname = 'alerts_status_check' "
                "AND conrelid = 'alerts'::regclass"
            ).fetchone()
            triggers = conn.execute(
                "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'case_reviews'::regclass "
                "AND tgname = 'case_reviews_append_only'"
            ).fetchone()
        assert oid is not None and oid == again and triggers == (1,)

    def test_upgrade_keeps_existing_alerts_and_only_adds_defaults(self, pg_dsn: str) -> None:
        from src.serving import store as st

        old = st.DDL.split("-- Analyst-owned fields")[0].replace("'investigating', ", "")
        with psycopg.connect(pg_dsn, autocommit=True) as conn:
            conn.execute(old)  # the schema before the dashboard (4 statuses, no version)
            check = conn.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'alerts_status_check' AND conrelid = 'alerts'::regclass"
            ).fetchone()
            assert check is not None and "investigating" not in check[0]
            conn.execute(
                "INSERT INTO scored_transactions VALUES "
                "(1, 10, 'c', 'u', '{}', 0.9, 0.5, true, 'v', '2026-10-01')"
            )
            conn.execute(
                "INSERT INTO alerts (transaction_id, score, status, created_at) "
                "VALUES (1, 0.9, 'closed', '2026-10-01')"
            )
            before = conn.execute("SELECT alert_id, transaction_id, score, status, created_at "
                                  "FROM alerts").fetchall()  # fmt: skip
            PostgresStore(pg_dsn).close()
            after = conn.execute(
                "SELECT alert_id, transaction_id, score, status, created_at, version FROM alerts"
            ).fetchall()
            conn.execute(
                "INSERT INTO scored_transactions VALUES "
                "(2, 11, 'c', 'u', '{}', 0.9, 0.5, true, 'v', now())"
            )
            conn.execute(  # the widened CHECK accepts the new state
                "INSERT INTO alerts (transaction_id, score, status) "
                "VALUES (2, 0.9, 'investigating')"
            )
        assert [r + (1,) for r in before] == after

    def test_case_reviews_is_append_only_but_truncate_works(
        self, dash: psycopg.Connection[Any]
    ) -> None:
        from src.dashboard import data

        alert = _alert_ids(dash)[9_000]
        data.change_status(dash, alert, 1, "investigating", "alice")
        for sql in ("UPDATE case_reviews SET notes = 'x'", "DELETE FROM case_reviews"):
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                dash.execute(sql)
        dash.execute("TRUNCATE case_reviews")  # row triggers do not fire on TRUNCATE
        assert dash.execute("SELECT count(*) FROM case_reviews").fetchone() == (0,)

    def test_submit_token_migration_is_additive(self, pg_dsn: str) -> None:
        """A database with reviews from before the token column: the setup adds a nullable
        column and a unique index, keeps every old row (token NULL) and stays idempotent."""
        from src.serving import store as st

        start = st.DDL.index("-- One-time token of the dashboard form")
        end = st.DDL.index("-- end of submit token")
        old = st.DDL[:start] + st.DDL[end:]
        column_sql = (
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'case_reviews' "
            "AND column_name = 'submit_token'"
        )
        review_cols = "id, alert_id, transaction_id, from_status, to_status, analyst, created_at"
        with psycopg.connect(pg_dsn, autocommit=True) as conn:
            conn.execute(old)  # the dashboard schema of f3d2815
            assert conn.execute(column_sql).fetchone() is None
            conn.execute(
                "INSERT INTO scored_transactions VALUES "
                "(1, 10, 'c', 'u', '{}', 0.9, 0.5, true, 'v', '2026-10-01')"
            )
            conn.execute("INSERT INTO alerts (transaction_id, score) VALUES (1, 0.9)")
            for status in ("investigating", "confirmed_fraud"):
                conn.execute(
                    "INSERT INTO case_reviews (alert_id, transaction_id, from_status, to_status, "
                    "analyst, alert_version) SELECT alert_id, 1, 'open', %s, 'alice', 2 "
                    "FROM alerts",
                    (status,),
                )
            before = conn.execute(f"SELECT {review_cols} FROM case_reviews ORDER BY id").fetchall()
            PostgresStore(pg_dsn).close()
            PostgresStore(pg_dsn).close()  # rerun: idempotent
            after = conn.execute(f"SELECT {review_cols} FROM case_reviews ORDER BY id").fetchall()
            tokens = conn.execute("SELECT submit_token FROM case_reviews ORDER BY id").fetchall()
            column = conn.execute(column_sql).fetchone()
            index = conn.execute(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
                "AND indexname = 'ux_case_reviews_submit_token'"
            ).fetchone()
        print(f"old reviews: before {len(before)} rows, after {len(after)} rows")
        assert before, "the 'before' side is empty: nothing to compare"
        assert after, "the 'after' side is empty: nothing to compare"
        assert after == before
        assert tokens == [(None,), (None,)]
        assert column == ("text", "YES")
        assert index is not None and "UNIQUE" in index[0] and "(submit_token)" in index[0]


@needs_db
class TestDashboardData:
    def test_customer_alerts_groups_by_uid_excludes_the_case_and_caps(
        self, dash: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dashboard import data

        ids = _alert_ids(dash)
        card0 = {ids[9_000 + i] for i in range(0, 30, 3)}  # uid "card0|addr": 10 alerts
        rows, total = data.customer_alerts(dash, "card0|addr", ids[9_000])
        print(f"customer alerts: {len(rows)} rows of {total}")
        assert total == 9 and len(rows) == 9
        assert {r["alert_id"] for r in rows} == card0 - {ids[9_000]}
        assert [r["alert_id"] for r in rows][:2] == [ids[9_027], ids[9_024]]  # score desc
        assert rows[0] == {
            "alert_id": ids[9_027],
            "score": pytest.approx(0.77),
            "status": "open",
            "transaction_day": 127,
        }
        monkeypatch.setattr(data, "MAX_CUSTOMER_ALERTS", 4)
        capped, total = data.customer_alerts(dash, "card0|addr", ids[9_000])
        assert (len(capped), total) == (4, 9)
        assert data.customer_alerts(dash, None, ids[9_000]) == ([], 0)
        assert data.customer_alerts(dash, "no-such-uid", ids[9_000]) == ([], 0)

    def test_change_status_bumps_version_and_appends_a_review(
        self, dash: psycopg.Connection[Any]
    ) -> None:
        from src.dashboard import data

        alert = _alert_ids(dash)[9_029]
        assert data.change_status(dash, alert, 1, "investigating", " alice ", "  ") == 2
        assert data.change_status(dash, alert, 2, "confirmed_fraud", "bob", "card testing") == 3
        status = dash.execute(
            "SELECT status, version, updated_at > created_at FROM alerts WHERE alert_id = %s",
            (alert,),
        ).fetchone()
        assert status == ("confirmed_fraud", 3, True)
        history = data.review_history(dash, alert)
        assert [(h["from_status"], h["to_status"], h["label"], h["analyst"], h["notes"],
                 h["alert_version"]) for h in history] == [
            ("open", "investigating", None, "alice", None, 2),
            ("investigating", "confirmed_fraud", "fraud", "bob", "card testing", 3),
        ]  # fmt: skip

    def test_optimistic_lock_conflict_is_rejected(self, dash: psycopg.Connection[Any]) -> None:
        from src.dashboard import data

        alert = _alert_ids(dash)[9_001]
        data.change_status(dash, alert, 1, "confirmed_fraud", "alice")
        with pytest.raises(data.StaleAlertError) as err:
            data.change_status(dash, alert, 1, "false_positive", "bob")  # stale version 1
        assert (err.value.current_status, err.value.current_version) == ("confirmed_fraud", 2)
        assert err.value.changed_by == "alice" and "nothing was saved" in str(err.value)
        assert [h["analyst"] for h in data.review_history(dash, alert)] == ["alice"]

    def test_invalid_or_anonymous_changes_write_nothing(
        self, dash: psycopg.Connection[Any]
    ) -> None:
        from src.dashboard import data, workflow

        alert = _alert_ids(dash)[9_002]
        with pytest.raises(workflow.InvalidTransitionError):
            data.change_status(dash, alert, 1, "closed", "alice")  # open -> closed not allowed
        with pytest.raises(ValueError, match="analyst"):
            data.change_status(dash, alert, 1, "investigating", "   ")
        with pytest.raises(data.AlertNotFoundError):
            data.change_status(dash, 10**9, 1, "investigating", "alice")
        assert dash.execute("SELECT status, version FROM alerts WHERE alert_id = %s",
                            (alert,)).fetchone() == ("open", 1)  # fmt: skip
        assert data.review_history(dash, alert) == []

    def test_resubmitting_the_same_token_writes_nothing(
        self, dash: psycopg.Connection[Any]
    ) -> None:
        """The incident replays passed the version check (the expected version was current).
        The token makes the same submit write at most once, even at the current version."""
        from src.dashboard import data

        ids = _alert_ids(dash)
        alert, other = ids[9_006], ids[9_007]
        version = data.change_status(dash, alert, 1, "confirmed_fraud", "alice", submit_token="t-1")
        assert version == 2
        before = _reviews(dash)
        with pytest.raises(data.DuplicateSubmitError, match="already saved"):
            data.change_status(dash, alert, 2, "false_positive", "alice", submit_token="t-1")
        with pytest.raises(data.DuplicateSubmitError):  # tokens are unique across alerts
            data.change_status(dash, other, 1, "investigating", "alice", submit_token="t-1")
        after = _reviews(dash)
        _assert_same_rows(before, after)
        assert _status(dash, alert) == ("confirmed_fraud", 2)
        assert _status(dash, other) == ("open", 1)
        stored = dash.execute(
            "SELECT submit_token FROM case_reviews WHERE alert_id = %s", (alert,)
        ).fetchall()
        assert stored == [("t-1",)]

    def test_same_token_from_concurrent_threads_writes_once(
        self, dash: psycopg.Connection[Any], pg_dsn: str
    ) -> None:
        """Different alerts are not serialised by the row lock: the unique index decides."""
        from src.dashboard import data
        from src.dashboard.db import Database

        db, ids = Database(pg_dsn), _alert_ids(dash)
        alerts = [ids[9_011 + i] for i in range(4)]
        barrier, results = threading.Barrier(4), []

        def writer(alert: int) -> None:
            barrier.wait(timeout=30)
            with db.connection() as conn:
                try:
                    data.change_status(conn, alert, 1, "investigating", "a", submit_token="same")
                    results.append("ok")
                except data.DuplicateSubmitError:
                    results.append("duplicate")

        threads = [threading.Thread(target=writer, args=(a,)) for a in alerts]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert sorted(results) == ["duplicate", "duplicate", "duplicate", "ok"]
        written = dash.execute(
            "SELECT alert_id FROM case_reviews WHERE submit_token = 'same'"
        ).fetchall()
        assert len(written) == 1
        assert sum(_status(dash, a) == ("investigating", 2) for a in alerts) == 1

    def test_a_save_with_a_stale_seen_version_writes_nothing(
        self, dash: psycopg.Connection[Any]
    ) -> None:
        from src.dashboard import data

        alert = _alert_ids(dash)[9_008]
        data.change_status(dash, alert, 1, "investigating", "bob", submit_token="bob-1")
        before = _reviews(dash)
        with pytest.raises(data.StaleAlertError, match="This case changed") as err:
            data.change_status(dash, alert, 1, "false_positive", "alice", submit_token="alice-1")
        assert "Reload the case" in str(err.value) and "nothing was saved" in str(err.value)
        after = _reviews(dash)
        _assert_same_rows(before, after)
        assert _status(dash, alert) == ("investigating", 2)

    def test_a_save_with_no_status_is_rejected(self, dash: psycopg.Connection[Any]) -> None:
        from src.dashboard import data

        alert = _alert_ids(dash)[9_009]
        data.change_status(dash, alert, 1, "investigating", "alice", submit_token="ok-1")
        before = _reviews(dash)
        no_status: tuple[Any, ...] = ("", "  ", None)  # None: what an empty dropdown sends
        for i, missing in enumerate(no_status):
            with pytest.raises(ValueError, match="Choose a new status"):
                data.change_status(dash, alert, 2, missing, "alice", submit_token=f"x{i}")
        after = _reviews(dash)
        _assert_same_rows(before, after)
        assert _status(dash, alert) == ("investigating", 2)

    def test_concurrent_threads_one_writer_wins_and_reads_are_safe(
        self, dash: psycopg.Connection[Any], pg_dsn: str
    ) -> None:
        from src.dashboard import data
        from src.dashboard.db import Database

        db, alert = Database(pg_dsn), _alert_ids(dash)[9_003]
        barrier = threading.Barrier(8)
        results: list[str] = []

        def writer(name: str) -> None:
            barrier.wait(timeout=30)
            with db.connection() as conn:
                try:
                    data.change_status(conn, alert, 1, "investigating", name)
                    results.append("ok")
                except data.StaleAlertError:
                    results.append("stale")

        def reader() -> None:
            barrier.wait(timeout=30)
            for _ in range(5):
                with db.connection() as conn:
                    assert data.list_alerts(conn, data.AlertFilter(), 1, 10).total == 30
                    assert data.overview(conn)["total"] == 30

        threads = [threading.Thread(target=writer, args=(f"a{i}",)) for i in range(4)]
        threads += [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert sorted(results) == ["ok", "stale", "stale", "stale"]
        assert len(data.review_history(dash, alert)) == 1

    def test_pagination_and_filters(self, dash: psycopg.Connection[Any]) -> None:
        from src.dashboard import data

        first = data.list_alerts(dash, data.AlertFilter(), 1, 12)
        third = data.list_alerts(dash, data.AlertFilter(), 3, 12)
        assert (first.total, len(first.rows), len(third.rows)) == (30, 12, 6)
        scores = [
            r["score"]
            for r in first.rows
            + data.list_alerts(dash, data.AlertFilter(), 2, 12).rows
            + third.rows
        ]
        assert scores == sorted(scores, reverse=True) and len(set(scores)) == 30
        assert data.list_alerts(dash, data.AlertFilter(), 1, 10_000).page_size == data.MAX_PAGE_SIZE
        rng = data.list_alerts(dash, data.AlertFilter(min_score=0.6, max_score=0.7), 1, 50)
        assert rng.total == 11 and all(0.6 <= r["score"] <= 0.7 for r in rng.rows)
        days = data.list_alerts(dash, data.AlertFilter(day_from=110, day_to=114), 1, 50)
        assert sorted(r["transaction_day"] for r in days.rows) == [110, 111, 112, 113, 114]
        data.change_status(dash, _alert_ids(dash)[9_010], 1, "investigating", "alice")
        inv = data.list_alerts(dash, data.AlertFilter(statuses=("investigating",)), 1, 50)
        assert [r["transaction_id"] for r in inv.rows] == [9_010]
        none = data.list_alerts(dash, data.AlertFilter(statuses=("closed",)), 1, 50)
        assert (none.total, none.rows) == (0, [])

    def test_latest_labels_use_the_latest_labelled_review(
        self, dash: psycopg.Connection[Any]
    ) -> None:
        from src.dashboard import data

        ids = _alert_ids(dash)
        data.change_status(dash, ids[9_020], 1, "confirmed_fraud", "alice")
        data.change_status(dash, ids[9_020], 2, "false_positive", "bob")  # correction
        data.change_status(dash, ids[9_021], 1, "investigating", "alice")  # no label yet
        data.change_status(dash, ids[9_022], 1, "false_positive", "alice")
        data.change_status(dash, ids[9_022], 2, "closed", "alice")  # closing keeps the label
        data.change_status(dash, ids[9_023], 1, "confirmed_fraud", "carol")
        labels = {r.transaction_id: r.label for r in data.latest_labels(dash)}
        assert labels == {9_020: "not_fraud", 9_022: "not_fraud", 9_023: "fraud"}
        ov = data.overview(dash)
        expected = dash.execute(  # the same definition, written by hand
            "SELECT count(*) FILTER (WHERE label = 'fraud')::float / count(*) FROM ("
            "  SELECT DISTINCT ON (transaction_id) label FROM case_reviews"
            "  WHERE label IS NOT NULL ORDER BY transaction_id, id DESC) l"
        ).fetchone()
        assert (ov["reviewed"], ov["confirmed_fraud"]) == (3, 1)
        assert ov["precision"] == pytest.approx(1 / 3) == expected[0]  # type: ignore[index]
        assert ov["by_status"] == {"open": 26, "false_positive": 1, "investigating": 1,
                                   "closed": 1, "confirmed_fraud": 1}  # fmt: skip

    def test_rings_link_by_uid_and_schema_check(self, dash: psycopg.Connection[Any]) -> None:
        from src.dashboard import data

        assert data.missing_schema(dash) == []
        dash.execute(
            "INSERT INTO fraud_rings VALUES (1, 7, 2, 2, 5, 100.0, '[\"dev\"]', 0.4, 5, 0.2, "
            '1, 2, \'["card1|addr", "zzz|1"]\', now())'
        )
        ring = data.ring_for_uid(dash, "card1|addr")
        assert ring is not None and ring["rank"] == 1
        assert (
            data.ring_for_uid(dash, "card0|addr") is None and data.ring_for_uid(dash, None) is None
        )
        members = data.ring_alerts(dash, ["card1|addr", "zzz|1"])
        assert len(members) == 10 and {m["uid"] for m in members} == {"card1|addr"}
        assert data.card_of_uid("1111|321|150|visa|226|debit|315") == "1111|321|150|visa|226|debit"

    def test_missing_schema_is_reported(self, pg_dsn: str) -> None:
        from src.dashboard import data

        with psycopg.connect(pg_dsn, autocommit=True) as conn:  # empty schema
            missing = data.missing_schema(conn)
            assert "case_reviews.id" in missing and "alerts.version" in missing
            with pytest.raises(data.SchemaNotReadyError, match="Start the API or a consumer"):
                data.require_schema(conn)

    def test_replayed_transaction_keeps_the_analyst_fields(
        self, dash: psycopg.Connection[Any], pg_store: PostgresStore, bundle_dir: Path
    ) -> None:
        """The one allowed scoring-path guarantee: a replay or retry of a reviewed transaction
        changes nothing on its alert (status, score, timestamps, version) or its reviews."""
        from src.dashboard import data

        alert = _alert_ids(dash)[9_005]
        data.change_status(dash, alert, 1, "confirmed_fraud", "alice", "reviewed")
        snapshot = (
            "SELECT status, score, created_at, updated_at, version FROM alerts WHERE alert_id = %s"
        )
        before = dash.execute(snapshot, (alert,)).fetchone()
        reviews = data.review_history(dash, alert)

        scorer = Scorer(ModelBundle.load(bundle_dir), pg_store)
        result = scorer.score_event(TransactionEvent.model_validate(event(TransactionID=9_005)))
        assert result.duplicate  # the normal replay path: dedupe, no write
        retried = replace(record(9_005, 105 * 86_400 + 5, alert=True), score=0.01)
        assert pg_store.save(retried) is False  # a forced retry past the dedupe check
        assert dash.execute(snapshot, (alert,)).fetchone() == before
        assert data.review_history(dash, alert) == reviews


# --------------------------------------------------------------------------- dashboard pages
# All five pages in Streamlit's AppTest on a real repository (ui.Repository on PostgreSQL), incl.
# a deployment where the batch jobs (fraud rings, drift) have not run (k8s/dashboard.yaml).

DASHBOARD_PAGES = (
    "views/overview.py",
    "views/alerts.py",
    "views/case.py",
    "views/rings.py",
    "views/drift.py",
)


def _render(dsn: str, reports_dir: Path, page: str, alert_id: int) -> Any:
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    from src.dashboard.db import Database
    from src.dashboard.ui import Repository

    app = str(Path(__file__).resolve().parents[1] / "src" / "dashboard" / "app.py")
    at = AppTest.from_file(app, default_timeout=60)
    at.session_state["repo"] = Repository(Database(dsn), reports_dir)
    at.session_state["alert_id"] = alert_id
    at.run()
    if page != "views/overview.py":
        at.switch_page(page)
        at.run()
    assert not at.exception, (page, [e.value for e in at.exception])
    return at


def _page_texts(at: Any) -> str:
    parts = [m.value for m in at.markdown] + [c.value for c in at.caption]
    parts += [str(i.value) for i in at.info] + [str(e.value) for e in at.error]
    parts += [t.value for t in at.title] + [h.value for h in at.subheader]
    return "\n".join(str(p) for p in parts)


def _tables(conn: psycopg.Connection[Any]) -> list[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
        "ORDER BY 1"
    ).fetchall()
    return [r[0] for r in rows]


def _drift_row(report_path: str) -> Any:
    from src.monitoring.report_store import DriftReportRow

    return DriftReportRow(
        synthetic=False, forced=False, top_gain_features=["card3"],
        drifted_top_gain_features=[], synthetic_note=None, now_dt=130 * 86_400,
        label_cutoff_dt=100 * 86_400, window_start_dt=123 * 86_400, window_end_dt=130 * 86_400,
        reference_start_dt=0, reference_end_dt=90 * 86_400, current_rows=30,
        reference_rows=1000, feature_count=28, drifted_feature_count=1,
        drifted_feature_share=1 / 28, top_drifted_features=[{"feature": "card3", "score": 0.1}],
        prediction_drift_method="psi", prediction_drift_score=0.01,
        prediction_drift_detected=False, performance_start_dt=0, performance_end_dt=0,
        delayed_rows=0, delayed_positives=0, delayed_pr_auc=None, champion_valid_pr_auc=0.5,
        triggered=False, trigger_reasons=[], retrain_run_id=None, challenger_version=None,
        promotion_decision=None, promotion=None, windows={}, report_path=report_path, config={},
    )  # fmt: skip


@needs_db
class TestDashboardPages:
    def test_core_tables_only_every_page_renders_and_says_what_has_not_run(
        self, pg_dsn: str, pg_store: PostgresStore, tmp_path: Path
    ) -> None:
        from src.dashboard import data, ui

        for i in range(2):
            assert pg_store.save(record(8_000 + i, (100 + i) * 86_400, alert=True))
        with psycopg.connect(pg_dsn, autocommit=True) as conn:
            before = _tables(conn)
            assert before == ["alerts", "case_reviews", "scored_transactions"]
            state = data.schema_state(conn)
            alert_id = _alert_ids(conn)[8_000]
        assert state.missing == () and state.absent == {"fraud_rings", "drift_reports"}

        pages = {p: _render(pg_dsn, tmp_path, p, alert_id) for p in DASHBOARD_PAGES}
        for page, at in pages.items():
            print(f"{page}: title={[t.value for t in at.title]} info={[i.value for i in at.info]}")
            assert not at.error, (page, [e.value for e in at.error])
            assert "schema is not initialised" not in _page_texts(at)
        assert [i.value for i in pages["views/rings.py"].info] == [ui.RINGS_NOT_RUN]
        assert pages["views/rings.py"].dataframe == []
        assert "No drift reports in this database yet" in _page_texts(pages["views/drift.py"])
        case = _page_texts(pages["views/case.py"])
        assert "Linked fraud ring" in case and ui.NOT_AVAILABLE in case
        assert ui.RINGS_NOT_RUN in case
        queue = pages["views/alerts.py"].dataframe[0].value
        print(f"queue rows: {len(queue)}")
        assert len(queue) == 2
        with psycopg.connect(pg_dsn, autocommit=True) as conn:
            assert _tables(conn) == before  # the dashboard never runs DDL

    def test_full_schema_pages_behave_as_before(
        self, dash: psycopg.Connection[Any], pg_dsn: str, tmp_path: Path
    ) -> None:
        from src.dashboard import data
        from src.monitoring.report_store import insert_report

        dash.execute(
            "INSERT INTO fraud_rings VALUES (1, 7, 2, 2, 5, 100.0, '[\"dev\"]', 0.4, 5, 0.2, "
            '1, 2, \'["card1|addr", "zzz|1"]\', now())'
        )
        (tmp_path / "drift_x.html").write_text("<html>evidently</html>", encoding="utf-8")
        insert_report(pg_dsn, _drift_row("/app/reports/drift/drift_x.html"))
        assert data.schema_state(dash) == data.SchemaState(missing=(), absent=frozenset())
        alert_id = _alert_ids(dash)[9_001]  # card1|addr: a ring member

        pages = {p: _render(pg_dsn, tmp_path, p, alert_id) for p in DASHBOARD_PAGES}
        for page, at in pages.items():
            assert not at.error, (page, [e.value for e in at.error])
        overview = {m.label: m.value for m in pages["views/overview.py"].metric}
        counts = {
            "overview open alerts": overview["Open"],
            "queue rows": len(pages["views/alerts.py"].dataframe[0].value),
            "rings rows": len(pages["views/rings.py"].dataframe[0].value),
            "drift rows": len(pages["views/drift.py"].dataframe[0].value),
        }
        print(counts)
        assert counts == {
            "overview open alerts": "30",
            "queue rows": 30,
            "rings rows": 1,
            "drift rows": 1,
        }
        case = _page_texts(pages["views/case.py"])
        assert "Linked fraud ring" in case and "Ring #1 (id 7)" in case
        assert "has not run" not in case
        assert "has not run" not in _page_texts(pages["views/rings.py"])

    @pytest.mark.parametrize(
        ("table", "column", "page"),
        [
            ("fraud_rings", "fraud_rate", "views/rings.py"),
            ("fraud_rings", "fraud_rate", "views/case.py"),
            ("drift_reports", "triggered", "views/drift.py"),
        ],
    )
    def test_optional_table_with_a_missing_column_fails_loudly(
        self,
        dash: psycopg.Connection[Any],
        pg_dsn: str,
        tmp_path: Path,
        table: str,
        column: str,
        page: str,
    ) -> None:
        from src.dashboard import data

        dash.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        state = data.schema_state(dash)
        assert state.missing == (f"{table}.{column}",) and state.absent == frozenset()

        at = _render(pg_dsn, tmp_path, page, _alert_ids(dash)[9_001])
        errors = [str(e.value) for e in at.error]
        print(f"{page}: {errors}")
        assert len(errors) == 1
        assert "schema is not initialised" in errors[0] and f"{table}.{column}" in errors[0]
        assert at.dataframe == [] and "has not run" not in _page_texts(at)


@needs_db
def test_benchmark_cleanup_works_on_a_clone_with_reviews() -> None:
    """scripts/bench_throughput.py on a fraud_bench that holds reviews (cloned from a demo
    database after analysts used the dashboard): FK + append-only trigger handled by TRUNCATE."""
    from scripts import bench_throughput as bench
    from src.dashboard import data

    assert DB_URL
    with psycopg.connect(DB_URL, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS fraud_bench WITH (FORCE)")
        admin.execute("CREATE DATABASE fraud_bench")
    dsn = make_conninfo(DB_URL, dbname="fraud_bench")
    try:
        store = PostgresStore(dsn)
        for i in range(6):
            store.save(record(7_000 + i, 86_400 * (1 + i), alert=True))
        store.close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            ids = _alert_ids(conn)
            data.change_status(conn, ids[7_004], 1, "confirmed_fraud", "alice")
            data.change_status(conn, ids[7_001], 1, "investigating", "alice")
            reviews, alerts, rows = bench.delete_replay_rows(conn, [7_003, 7_004, 7_005])
            assert (reviews, alerts, rows) == (2, 3, 3)
            left = conn.execute("SELECT transaction_id FROM scored_transactions ORDER BY 1")
            assert [r[0] for r in left.fetchall()] == [7_000, 7_001, 7_002]
            assert conn.execute("SELECT count(*) FROM case_reviews").fetchone() == (0,)
        with (  # the guard refuses any database other than fraud_bench
            psycopg.connect(DB_URL, autocommit=True) as other,
            pytest.raises(SystemExit, match="refused"),
        ):
            bench.delete_replay_rows(other, [1])
    finally:
        with psycopg.connect(DB_URL, autocommit=True) as admin:
            admin.execute("DROP DATABASE IF EXISTS fraud_bench WITH (FORCE)")
