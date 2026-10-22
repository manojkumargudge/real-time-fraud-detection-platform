from __future__ import annotations

import json
import logging
import shutil
import signal
import threading
from collections import Counter
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.data.load import DEFAULT_RAW_DIR, load_ieee_cis
from src.features.history import HistoryConfig
from src.models.compare import build_model_frame
from src.serving.api import create_app
from src.serving.config import Settings
from src.serving.model import ModelBundle, ModelBundleError
from src.serving.online_features import entity_keys, events_to_frame, online_model_row
from src.serving.schema import MAX_SAFE_INT, MAX_TRANSACTION_DT, ScoreResponse, TransactionEvent
from src.serving.scorer import Scorer
from src.serving.store import InMemoryStore, ScoredRecord
from src.streaming import consumer as consumer_mod
from src.streaming import producer as producer_mod
from src.streaming.events import decode, encode, row_to_event
from src.streaming.processor import process_message
from tests.kafka_fakes import NO_WAIT, FakeConsumer, FakeDeadLetterSink, FakeError, FakeMessage

NAMES = HistoryConfig().feature_names()
PARITY_EVENTS = 800
REAL_PARITY_EVENTS = 300


def event(**overrides: Any) -> dict[str, Any]:
    base = {
        "TransactionID": 1,
        "TransactionDT": 100_000,
        "TransactionAmt": 50.0,
        "ProductCD": "W",
        "card1": 1111,
        "card2": 321.0,
        "card4": "visa",
        "card6": "debit",
        "addr1": 315.0,
        "has_identity": False,
    }
    return {**base, **overrides}


@pytest.fixture(scope="module")
def bundle(bundle_dir: Path) -> ModelBundle:
    return ModelBundle.load(bundle_dir)


@pytest.fixture
def scorer(bundle: ModelBundle) -> Scorer:
    return Scorer(bundle, InMemoryStore())


def memory_store(scorer: Scorer) -> InMemoryStore:
    assert isinstance(scorer.store, InMemoryStore)
    return scorer.store


def replay_events(frame: pd.DataFrame) -> list[dict[str, Any]]:
    ordered = frame.sort_values(["TransactionDT", "TransactionID"], kind="stable")
    return [
        row_to_event({str(k): v for k, v in r.items()}) for r in ordered.to_dict(orient="records")
    ]


class TestSchema:
    def test_valid_event(self) -> None:
        assert TransactionEvent.model_validate(event()).TransactionID == 1

    @pytest.mark.parametrize("field", ["isFraud", "D1", "uid", "card_id"])
    def test_label_d1_and_unknown_fields_rejected(self, field: str) -> None:
        with pytest.raises(ValidationError, match="Extra inputs"):
            TransactionEvent.model_validate(event(**{field: 1}))

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("TransactionAmt", -1.0),
            ("TransactionAmt", float("nan")),
            ("TransactionAmt", float("inf")),
            ("TransactionDT", -5),
            ("TransactionID", "abc"),
            ("card4", "x" * 200),
        ],
    )
    def test_invalid_values(self, field: str, value: Any) -> None:
        with pytest.raises(ValidationError):
            TransactionEvent.model_validate(event(**{field: value}))

    @pytest.mark.parametrize("field", ["TransactionID", "TransactionDT", "TransactionAmt"])
    def test_required_fields(self, field: str) -> None:
        data = event()
        del data[field]
        with pytest.raises(ValidationError):
            TransactionEvent.model_validate(data)

    def test_device_fields_require_identity(self) -> None:
        with pytest.raises(ValidationError, match="has_identity"):
            TransactionEvent.model_validate(event(DeviceInfo="iOS"))
        assert TransactionEvent.model_validate(event(DeviceInfo="iOS", has_identity=True))

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("TransactionID", MAX_SAFE_INT + 1),
            ("TransactionID", 2**63),  # beyond PostgreSQL BIGINT
            ("TransactionDT", MAX_TRANSACTION_DT + 1),
            ("TransactionDT", 1_700_000_000_000),  # epoch milliseconds, not seconds
            ("card1", 10**30),
            ("card1", 10**400),
            ("ProductCD", "W\x00"),
            ("card4", "\x00"),
        ],
    )
    def test_unscoreable_values_rejected(self, field: str, value: Any) -> None:
        """Regression: these passed validation, then failed in scoring or storage every time."""
        with pytest.raises(ValidationError):
            TransactionEvent.model_validate(event(**{field: value}))

    def test_nul_rejected_in_identity_fields(self) -> None:
        with pytest.raises(ValidationError, match="NUL"):
            TransactionEvent.model_validate(event(DeviceInfo="iOS\x00", has_identity=True))

    def test_boundary_values_are_scoreable(self, scorer: Scorer) -> None:
        """Every accepted extreme must score: IDs at the bound survive the float64 path."""
        first = scorer.score_event(
            TransactionEvent.model_validate(event(TransactionID=MAX_SAFE_INT - 1, TransactionDT=0))
        )
        last = scorer.score_event(
            TransactionEvent.model_validate(
                event(TransactionID=MAX_SAFE_INT, TransactionDT=MAX_TRANSACTION_DT)
            )
        )
        assert first.transaction_id == MAX_SAFE_INT - 1 and last.transaction_id == MAX_SAFE_INT
        assert last.history_rows == 1  # same card, so the earlier event is its history


class TestModelBundle:
    def test_loads_and_predicts_probabilities(self, bundle: ModelBundle) -> None:
        row = online_model_row(event(), [], bundle.history_config)
        p = bundle.predict(row)
        assert p.shape == (1,) and 0.0 <= p[0] <= 1.0
        assert bundle.model_version.startswith("baseline-")

    def copy_bundle(self, bundle_dir: Path, tmp_path: Path) -> Path:
        dst = tmp_path / "b"
        shutil.copytree(bundle_dir, dst)
        return dst

    def test_missing_files(self, tmp_path: Path) -> None:
        with pytest.raises(ModelBundleError, match="src.models.train"):
            ModelBundle.load(tmp_path)

    def test_tampered_model_rejected(self, bundle_dir: Path, tmp_path: Path) -> None:
        d = self.copy_bundle(bundle_dir, tmp_path)
        (d / "model.json").write_text((d / "model.json").read_text() + " ", encoding="utf-8")
        with pytest.raises(ModelBundleError, match="sha256"):
            ModelBundle.load(d)

    @pytest.mark.parametrize(
        ("key", "value", "match"),
        [
            ("feature_columns", ["TransactionAmt"], "feature schema"),
            ("variant", "candidate", "baseline"),
            ("schema_version", 99, "schema_version"),
            ("threshold", float("nan"), "threshold"),  # would silently disable all alerts
            ("threshold", 1.5, "threshold"),
            ("threshold", -0.1, "threshold"),
        ],
    )
    def test_incompatible_metadata(
        self, bundle_dir: Path, tmp_path: Path, key: str, value: Any, match: str
    ) -> None:
        d = self.copy_bundle(bundle_dir, tmp_path)
        meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
        meta[key] = value
        (d / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        with pytest.raises(ModelBundleError, match=match):
            ModelBundle.load(d)


class TestTrainServeParity:
    def test_online_features_and_scores_match_batch(
        self, synthetic_frame: pd.DataFrame, bundle: ModelBundle
    ) -> None:
        """Replay events in time order; online == batch features and scores, row by row."""
        cfg = bundle.history_config
        events = replay_events(synthetic_frame)[:PARITY_EVENTS]
        ids = [e["TransactionID"] for e in events]
        # Batch features use the full file, but only strictly earlier rows, so the first N
        # events' batch features are exactly what N replayed events can know.
        batch = build_model_frame(synthetic_frame, "baseline", cfg).set_index("TransactionID")
        batch = batch.loc[ids]
        batch_scores = bundle.predict(batch)

        scorer = Scorer(bundle, InMemoryStore())
        results = [scorer.score_event(TransactionEvent.model_validate(e)) for e in events]
        online = pd.DataFrame([r.history_features for r in results], index=ids, columns=NAMES)

        pd.testing.assert_frame_equal(
            online, batch[NAMES].astype("float64"), check_names=False, check_index_type=False
        )
        np.testing.assert_allclose([r.score for r in results], batch_scores, rtol=1e-6)
        assert sum(r.history_rows > 0 for r in results) > PARITY_EVENTS // 4  # history exercised

    def test_future_events_in_store_are_never_used(self, bundle: ModelBundle) -> None:
        scorer = Scorer(bundle, InMemoryStore())
        for i, dt in enumerate((500_000, 400_000), start=10):  # later events arrive first
            scorer.score_event(
                TransactionEvent.model_validate(event(TransactionID=i, TransactionDT=dt))
            )
        early = event(TransactionID=1, TransactionDT=100_000)
        card_id, _ = entity_keys(early)
        assert card_id is not None
        history = scorer.store.card_history(card_id, 100_000)
        row = online_model_row(early, history, bundle.history_config)
        assert history == [] and row["uid_prior_txn_count"].iloc[0] == 0

    def test_defensive_filter_drops_future_and_self(self, bundle: ModelBundle) -> None:
        ev = event(TransactionID=5, TransactionDT=1_000)
        leaked = [
            event(TransactionID=6, TransactionDT=2_000),
            event(TransactionID=5, TransactionDT=900),
        ]
        row = online_model_row(ev, leaked, bundle.history_config)
        assert row["uid_prior_txn_count"].iloc[0] == 0

    def test_missing_card1_scores_without_history(self, scorer: Scorer) -> None:
        scorer.score_event(TransactionEvent.model_validate(event(TransactionID=1, card1=None)))
        res = scorer.score_event(
            TransactionEvent.model_validate(
                event(TransactionID=2, card1=None, TransactionDT=200_000)
            )
        )
        assert res.history_rows == 0 and 0 <= res.score <= 1


class TestParityEdgeCases:
    """Exact parity holds for chronological replay; late arrival and cold start diverge."""

    def replay(
        self, bundle: ModelBundle, arrivals: list[dict[str, Any]]
    ) -> dict[int, dict[str, float | None]]:
        scorer = Scorer(bundle, InMemoryStore())
        out = {}
        for e in arrivals:
            res = scorer.score_event(TransactionEvent.model_validate(e))
            out[res.transaction_id] = res.history_features
        return out

    def batch(
        self, bundle: ModelBundle, events: list[dict[str, Any]]
    ) -> dict[int, dict[str, float | None]]:
        records = [TransactionEvent.model_validate(e).to_record() for e in events]
        frame = build_model_frame(events_to_frame(records), "baseline", bundle.history_config)
        feats = frame.set_index("TransactionID")[NAMES].astype("float64")
        return {
            int(cast(int, i)): {
                cast(str, k): (None if np.isnan(v) else float(v)) for k, v in row.items()
            }
            for i, row in feats.iterrows()
        }

    EDGE_EVENTS = [
        event(TransactionID=1, TransactionDT=1_000, DeviceInfo="a", has_identity=True),
        event(TransactionID=2, TransactionDT=1_000, DeviceInfo="b", has_identity=True),  # tie
        event(TransactionID=3, TransactionDT=1_000, card1=None),  # missing entity key
        event(TransactionID=4, TransactionDT=2_000, addr1=None),  # coarse uid, same card_id
        event(TransactionID=5, TransactionDT=3_000, DeviceInfo="a", has_identity=True),
        event(TransactionID=6, TransactionDT=3_000, card1=2222),  # unseen entity
        event(TransactionID=7, TransactionDT=4_600, TransactionAmt=500.0),  # exactly 1h after 1, 2
        event(TransactionID=8, TransactionDT=4_600, card1=None),
    ]

    @pytest.mark.parametrize("tie_order", ["by_id", "reversed"])
    def test_chronological_replay_matches_batch_on_edge_cases(
        self, bundle: ModelBundle, tie_order: str
    ) -> None:
        arrivals = sorted(
            self.EDGE_EVENTS,
            key=lambda e: (
                e["TransactionDT"],
                e["TransactionID"] if tie_order == "by_id" else -e["TransactionID"],
            ),
        )
        assert self.replay(bundle, arrivals) == self.batch(bundle, self.EDGE_EVENTS)

    def test_edge_case_values(self, bundle: ModelBundle) -> None:
        f = self.batch(bundle, self.EDGE_EVENTS)
        assert f[1]["uid_prior_txn_count"] == f[2]["uid_prior_txn_count"] == 0  # ties unseen
        assert f[3]["uid_prior_txn_count"] is None  # no card1 -> no entity -> NaN
        assert f[4]["uid_prior_txn_count"] == 0  # addr1 missing -> different uid
        assert f[4]["card_addr1_changed"] is None  # current addr1 unknown -> no flag
        # Tied rows at t=1000 are ordered by TransactionID, so the last known device is "b"
        # (id 2), not "a"; the device-less t=2000 row does not overwrite it.
        assert f[5]["uid_prior_txn_count"] == 2 and f[5]["card_deviceinfo_changed"] == 1.0
        assert f[6]["uid_prior_txn_count"] == 0 and f[6]["uid_prior_amt_mean"] is None
        assert f[7]["uid_txn_count_1h"] == 3  # [1000, 4600) includes the t=1000 ties and t=3000

    def test_late_arrival_diverges_from_batch_without_using_future_data(
        self, bundle: ModelBundle
    ) -> None:
        x, y, z = (event(TransactionID=i, TransactionDT=100 * i) for i in (1, 2, 3))
        batch = self.batch(bundle, [x, y, z])
        online = self.replay(bundle, [y, x, z])  # x arrives after y was already scored
        assert batch[2]["uid_prior_txn_count"] == 1
        assert online[2]["uid_prior_txn_count"] == 0  # y never sees late x: a known divergence
        assert online[1] == batch[1]  # x is not affected by the already-stored, later y
        assert online[3] == batch[3]  # z arrives after both and matches batch exactly

    def test_cold_start_has_no_history_until_replayed(self, bundle: ModelBundle) -> None:
        history = [event(TransactionID=i, TransactionDT=100 * i) for i in (1, 2)]
        scored = event(TransactionID=3, TransactionDT=300)
        assert self.batch(bundle, [*history, scored])[3]["uid_prior_txn_count"] == 2
        assert self.replay(bundle, [scored])[3]["uid_prior_txn_count"] == 0  # empty store
        assert self.replay(bundle, [*history, scored])[3]["uid_prior_txn_count"] == 2


class TestDuplicates:
    def test_same_event_twice_is_stored_once(self, scorer: Scorer) -> None:
        e = TransactionEvent.model_validate(event())
        first, second = scorer.score_event(e), scorer.score_event(e)
        assert not first.duplicate and second.duplicate
        assert second.score == first.score and len(memory_store(scorer)) == 1

    def test_conflicting_payload_returns_stored_result(
        self, scorer: Scorer, caplog: pytest.LogCaptureFixture
    ) -> None:
        first = scorer.score_event(TransactionEvent.model_validate(event()))
        with caplog.at_level(logging.WARNING):
            again = scorer.score_event(
                TransactionEvent.model_validate(event(TransactionAmt=9999.0))
            )
        assert again.duplicate and again.score == first.score
        assert "different payload" in caplog.text

    def test_alert_recorded_once(self, bundle: ModelBundle) -> None:
        store = InMemoryStore()
        alerting = Scorer(
            ModelBundle(**{**bundle.__dict__, "threshold": 0.0}), store
        )  # every score is an alert
        e = TransactionEvent.model_validate(event())
        alerting.score_event(e)
        alerting.score_event(e)
        assert list(store.alerts) == [1]


class TestApi:
    def client(self, scorer: Scorer) -> TestClient:
        return TestClient(create_app(scorer))

    def test_health(self, scorer: Scorer) -> None:
        with self.client(scorer) as c:
            body = c.get("/health").json()
        assert body["status"] == "ok" and body["store"] == "memory"
        assert body["model_version"] == scorer.bundle.model_version

    def test_health_degraded_when_store_down(self, scorer: Scorer) -> None:
        scorer.store.ping = lambda: False  # type: ignore[method-assign]
        with self.client(scorer) as c:
            r = c.get("/health")
        assert r.status_code == 503 and r.json()["store_ok"] is False

    def test_score_and_duplicate(self, scorer: Scorer) -> None:
        with self.client(scorer) as c:
            a = c.post("/score", json=event()).json()
            b = c.post("/score", json=event()).json()
        assert a["duplicate"] is False and b["duplicate"] is True
        assert 0 <= a["score"] <= 1 and a["is_alert"] == (a["score"] >= a["threshold"])

    @pytest.mark.parametrize(
        "bad",
        [event(isFraud=1), event(TransactionAmt=-3), {"x": 1}, event(TransactionID=2**63)],
    )
    def test_invalid_payload_is_422_and_not_stored(
        self, scorer: Scorer, bad: dict[str, Any]
    ) -> None:
        with self.client(scorer) as c:
            assert c.post("/score", json=bad).status_code == 422
        assert len(memory_store(scorer)) == 0

    @pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_json_number_is_422_not_500(self, scorer: Scorer, literal: str) -> None:
        """Regression: the default 422 body echoed the NaN input and failed to render (500)."""
        body = json.dumps(event()).replace('"TransactionAmt": 50.0', f'"TransactionAmt": {literal}')
        assert literal in body
        with self.client(scorer) as c:
            r = c.post("/score", content=body, headers={"Content-Type": "application/json"})
        assert r.status_code == 422
        (err,) = r.json()["detail"]
        assert err["loc"] == ["body", "TransactionAmt"] and "finite" in err["msg"]
        assert len(memory_store(scorer)) == 0

    def test_validation_error_shape_is_unchanged_for_ordinary_errors(self, scorer: Scorer) -> None:
        with self.client(scorer) as c:
            r = c.post("/score", json=event(TransactionAmt=-3))
        (err,) = r.json()["detail"]
        assert r.status_code == 422 and err["loc"] == ["body", "TransactionAmt"]
        assert err["input"] == -3 and err["type"] == "greater_than_equal"

    def test_shutdown_closes_the_store_it_opened(
        self, bundle_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = InMemoryStore()
        closed: list[bool] = []
        store.close = lambda: closed.append(True)  # type: ignore[method-assign]
        monkeypatch.setenv("MODEL_DIR", str(bundle_dir))
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setattr(Settings, "build_store", lambda _self: store)
        with TestClient(create_app()) as c:
            assert c.get("/health").status_code == 200
            assert closed == []
        assert closed == [True]

    def test_injected_scorer_store_is_left_open(self, scorer: Scorer) -> None:
        closed: list[bool] = []
        scorer.store.close = lambda: closed.append(True)  # type: ignore[method-assign]
        with self.client(scorer) as c:
            c.get("/health")
        assert closed == []

    def test_self_built_app_configures_application_logging(
        self, bundle_dir: Path, scorer: Scorer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: under uvicorn the src.* INFO logs (model, store, scores) were dropped."""
        calls: list[dict[str, Any]] = []
        monkeypatch.setattr(logging, "basicConfig", lambda **kw: calls.append(kw))
        monkeypatch.setenv("MODEL_DIR", str(bundle_dir))
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with TestClient(create_app()) as c:
            assert c.get("/health").status_code == 200
        assert len(calls) == 1 and calls[0]["level"] == logging.INFO
        with self.client(scorer) as c:  # injected scorer: caller owns logging setup
            c.get("/health")
        assert len(calls) == 1


class TestEvents:
    def test_row_to_event_drops_label_d1_and_normalises_nan(self) -> None:
        row = {
            **event(),
            "isFraud": 1,
            "D1": 3.0,
            "V1": 0.5,
            "card2": np.float32("nan"),
            "card1": np.int64(1111),
        }
        out = row_to_event(row)
        assert "isFraud" not in out and "D1" not in out and "V1" not in out
        assert out["card2"] is None and out["card1"] == 1111

    def test_encode_decode_round_trip_keyed_by_card(self) -> None:
        key, value = encode(event())
        assert key.decode() == entity_keys(event())[0]
        assert decode(value).to_record() == TransactionEvent.model_validate(event()).to_record()

    def test_event_without_card1_keyed_by_id(self) -> None:
        key, _ = encode(event(card1=None, TransactionID=77))
        assert key == b"id:77"


class TestProcessor:
    def test_scored_then_duplicate(self, scorer: Scorer) -> None:
        _, value = encode(event())
        assert process_message(value, scorer).status == "scored"
        assert process_message(value, scorer).status == "duplicate"

    @pytest.mark.parametrize(
        "value",
        [None, b"not json", b"[1,2]", json.dumps(event(isFraud=0)).encode(), b"\xff\xfe"],
    )
    def test_invalid_messages_are_skipped(self, scorer: Scorer, value: bytes | None) -> None:
        out = process_message(value, scorer)
        assert out.status == "invalid" and out.error
        assert len(memory_store(scorer)) == 0

    def test_infrastructure_errors_propagate(self, scorer: Scorer) -> None:
        def boom(transaction_id: int) -> ScoredRecord | None:
            raise ConnectionError("db down")

        scorer.store.get = boom  # type: ignore[method-assign]
        with pytest.raises(ConnectionError):
            process_message(encode(event())[1], scorer)


class TestConsumerLoop:
    def test_commits_every_processed_message_including_invalid(self, scorer: Scorer) -> None:
        good = encode(event())[1]
        fake = FakeConsumer([good, good, b"junk", encode(event(TransactionID=2))[1]])
        dlq = FakeDeadLetterSink()
        counts = consumer_mod.run(fake, scorer, "t", dead_letters=dlq, max_messages=4)
        assert counts["scored"] == 2 and counts["duplicate"] == 1 and counts["invalid"] == 1
        assert len(fake.committed) == 4 and fake.closed and len(memory_store(scorer)) == 2
        assert [d.value for d in dlq.sent] == [b"junk"]  # the poison message, dead-lettered

    def test_no_commit_while_the_database_is_down(self, scorer: Scorer) -> None:
        """A transient outage is retried past the permanent-error limit, never dead-lettered,
        and the offset stays uncommitted when the consumer is stopped mid-outage."""
        stop = threading.Event()
        calls: list[int] = []

        def db_down(record: ScoredRecord) -> bool:
            calls.append(1)
            if len(calls) == 6:
                stop.set()  # e.g. docker stop during the outage
            raise ConnectionError("db down")

        scorer.store.save = db_down  # type: ignore[method-assign]
        fake = FakeConsumer([encode(event())[1]])
        dlq = FakeDeadLetterSink()
        counts = consumer_mod.run(
            fake, scorer, "t", dead_letters=dlq, policy=NO_WAIT, max_messages=1, stop=stop
        )
        assert (
            len(calls) == 6 and counts["retries"] == 6 and counts["interrupted"] == 1
        )  # 6th: wait cut
        assert fake.committed == [] and dlq.sent == [] and fake.closed

    def test_missing_topic_is_waited_out_not_fatal(self, scorer: Scorer) -> None:
        from confluent_kafka import KafkaError

        fake = FakeConsumer([])
        fake.queue = [
            FakeMessage(None, FakeError(KafkaError.UNKNOWN_TOPIC_OR_PART)),
            FakeMessage(encode(event())[1]),
        ]
        counts = consumer_mod.run(
            fake, scorer, "t", dead_letters=FakeDeadLetterSink(), max_messages=1
        )
        assert counts["scored"] == 1 and len(fake.committed) == 1

    def test_other_broker_errors_are_fatal(self, scorer: Scorer) -> None:
        from confluent_kafka import KafkaError, KafkaException

        fake = FakeConsumer([])
        fake.queue = [FakeMessage(None, FakeError(KafkaError._TRANSPORT))]
        with pytest.raises(KafkaException):
            consumer_mod.run(fake, scorer, "t", dead_letters=FakeDeadLetterSink(), max_messages=1)
        assert fake.committed == [] and fake.closed

    def test_idle_timeout_stops(self, scorer: Scorer) -> None:
        counts = consumer_mod.run(
            FakeConsumer([]), scorer, "t", dead_letters=FakeDeadLetterSink(), idle_timeout=0
        )
        assert sum(counts.values()) == 0

    def test_alerts_do_not_count_towards_max_messages(self) -> None:
        """Regression: the "alerts" tally was summed with statuses, stopping the run early."""
        values = [encode(event(TransactionID=i))[1] for i in range(1, 5)]
        fake = FakeConsumer(values)
        counts = consumer_mod.run(
            fake,
            AlertingScorer(),  # type: ignore[arg-type]
            "t",
            dead_letters=FakeDeadLetterSink(),
            max_messages=4,
        )
        assert counts["scored"] == 4 and counts["alerts"] == 4
        assert len(fake.committed) == 4 and fake.queue == []

    def test_main_turns_sigterm_into_a_graceful_stop(
        self, bundle_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: as PID 1 in Docker, Python ignored SIGTERM without a handler, so
        `docker stop` always ended in SIGKILL after the grace period (measured: exit 137)."""
        handlers: dict[int, Any] = {}
        monkeypatch.setattr(signal, "signal", lambda s, h: handlers.__setitem__(s, h))
        store = InMemoryStore()
        closed: list[bool] = []
        store.close = lambda: closed.append(True)  # type: ignore[method-assign]
        monkeypatch.setenv("MODEL_DIR", str(bundle_dir))
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setattr(Settings, "build_store", lambda _self: store)
        monkeypatch.setattr(consumer_mod, "build_consumer", lambda _s: FakeConsumer([]))
        monkeypatch.setattr(consumer_mod, "build_dead_letter_sink", lambda _s: FakeDeadLetterSink())

        def fake_run(*_args: Any, stop: threading.Event, **_kwargs: Any) -> Counter[str]:
            assert not stop.is_set()
            handlers[signal.SIGTERM](signal.SIGTERM, None)  # what `docker stop` delivers
            assert stop.is_set()
            return Counter()

        monkeypatch.setattr(consumer_mod, "run", fake_run)
        consumer_mod.main([])
        assert {signal.SIGTERM, signal.SIGINT} <= set(handlers) and closed == [True]

    def test_stop_event_ends_the_loop_and_closes(self, scorer: Scorer) -> None:
        stop = threading.Event()
        stop.set()
        fake = FakeConsumer([encode(event())[1]])
        counts = consumer_mod.run(fake, scorer, "t", dead_letters=FakeDeadLetterSink(), stop=stop)
        assert sum(counts.values()) == 0 and fake.committed == [] and fake.closed
        assert len(fake.queue) == 1  # nothing consumed, so nothing lost


class AlertingScorer:
    """Scores every event as a fresh alert (consumer accounting only, no model)."""

    def score_event(self, ev: TransactionEvent) -> ScoreResponse:
        return ScoreResponse(
            transaction_id=ev.TransactionID,
            score=0.99,
            is_alert=True,
            threshold=0.5,
            model_version="test",
            duplicate=False,
            history_rows=0,
            latency_ms=0.0,
        )


class FakeProducer:
    def __init__(self, undelivered: int) -> None:
        self.undelivered = undelivered
        self.produced = 0

    def produce(self, topic: str, **_kwargs: Any) -> None:
        self.produced += 1

    def poll(self, _timeout: float) -> int:
        return 0

    def flush(self, _timeout: float) -> int:
        return self.undelivered


class TestProducer:
    def test_publish_counts_delivered_events(self) -> None:
        fake = FakeProducer(undelivered=0)
        assert producer_mod.publish(fake, "t", [event(), event(TransactionID=2)]) == 2

    def test_messages_left_after_flush_are_an_error(self) -> None:
        """Regression: flush() timing out with queued messages used to report success."""
        with pytest.raises(RuntimeError, match="undelivered"):
            producer_mod.publish(FakeProducer(undelivered=1), "t", [event()])


@pytest.mark.real_data
@pytest.mark.skipif(
    not (DEFAULT_RAW_DIR / "train_transaction.csv").is_file(),
    reason="IEEE-CIS dataset not downloaded",
)
def test_real_data_online_replay_matches_batch(bundle: ModelBundle) -> None:
    """First N real transactions replayed in time order == batch features from the full file."""
    df = load_ieee_cis(DEFAULT_RAW_DIR, usecols=[])
    cfg = bundle.history_config
    events = replay_events(df.iloc[: REAL_PARITY_EVENTS * 2])[:REAL_PARITY_EVENTS]
    ids = [e["TransactionID"] for e in events]
    batch = build_model_frame(df, "baseline", cfg).set_index("TransactionID").loc[ids]

    scorer = Scorer(bundle, InMemoryStore())
    results = [scorer.score_event(TransactionEvent.model_validate(e)) for e in events]
    online = pd.DataFrame([r.history_features for r in results], index=ids, columns=NAMES)
    pd.testing.assert_frame_equal(
        online, batch[NAMES].astype("float64"), check_names=False, check_index_type=False
    )
    np.testing.assert_allclose([r.score for r in results], bundle.predict(batch), rtol=1e-6)
