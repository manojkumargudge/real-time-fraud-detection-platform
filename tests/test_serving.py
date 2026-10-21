from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.data.load import DEFAULT_RAW_DIR, load_ieee_cis
from src.features.history import HistoryConfig
from src.models.compare import build_model_frame
from src.serving.api import create_app
from src.serving.model import ModelBundle, ModelBundleError
from src.serving.online_features import entity_keys, events_to_frame, online_model_row
from src.serving.schema import TransactionEvent
from src.serving.scorer import Scorer
from src.serving.store import InMemoryStore
from src.streaming import consumer as consumer_mod
from src.streaming.events import decode, encode, row_to_event
from src.streaming.processor import process_message

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


def replay_events(frame: pd.DataFrame) -> list[dict[str, Any]]:
    ordered = frame.sort_values(["TransactionDT", "TransactionID"], kind="stable")
    return [row_to_event(r) for r in ordered.to_dict(orient="records")]


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
        history = scorer.store.card_history(entity_keys(early)[0], 100_000)
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

    def replay(self, bundle: ModelBundle, arrivals: list[dict[str, Any]]) -> dict[int, dict]:
        scorer = Scorer(bundle, InMemoryStore())
        out = {}
        for e in arrivals:
            res = scorer.score_event(TransactionEvent.model_validate(e))
            out[res.transaction_id] = res.history_features
        return out

    def batch(self, bundle: ModelBundle, events: list[dict[str, Any]]) -> dict[int, dict]:
        records = [TransactionEvent.model_validate(e).to_record() for e in events]
        frame = build_model_frame(events_to_frame(records), "baseline", bundle.history_config)
        feats = frame.set_index("TransactionID")[NAMES].astype("float64")
        return {
            int(i): {k: (None if np.isnan(v) else float(v)) for k, v in row.items()}
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
        assert second.score == first.score and len(scorer.store) == 1

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

    @pytest.mark.parametrize("bad", [event(isFraud=1), event(TransactionAmt=-3), {"x": 1}])
    def test_invalid_payload_is_422_and_not_stored(self, scorer: Scorer, bad: dict) -> None:
        with self.client(scorer) as c:
            assert c.post("/score", json=bad).status_code == 422
        assert len(scorer.store) == 0


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
        assert len(scorer.store) == 0

    def test_infrastructure_errors_propagate(self, scorer: Scorer) -> None:
        def boom(*_: Any) -> None:
            raise ConnectionError("db down")

        scorer.store.get = boom  # type: ignore[method-assign]
        with pytest.raises(ConnectionError):
            process_message(encode(event())[1], scorer)


class FakeError:
    def __init__(self, code: int) -> None:
        self._code = code

    def code(self) -> int:
        return self._code


class FakeMessage:
    def __init__(self, value: bytes | None, error: FakeError | None = None) -> None:
        self._value = value
        self._error = error

    def value(self) -> bytes | None:
        return self._value

    def error(self) -> FakeError | None:
        return self._error


class FakeConsumer:
    def __init__(self, values: list[bytes | None]) -> None:
        self.queue = [FakeMessage(v) for v in values]
        self.committed: list[FakeMessage] = []
        self.closed = False

    def subscribe(self, topics: list[str]) -> None:
        self.topics = topics

    def poll(self, _timeout: float) -> FakeMessage | None:
        return self.queue.pop(0) if self.queue else None

    def commit(self, message: FakeMessage, asynchronous: bool) -> None:
        assert asynchronous is False
        self.committed.append(message)

    def close(self) -> None:
        self.closed = True


class TestConsumerLoop:
    def test_commits_every_processed_message_including_invalid(self, scorer: Scorer) -> None:
        good = encode(event())[1]
        fake = FakeConsumer([good, good, b"junk", encode(event(TransactionID=2))[1]])
        counts = consumer_mod.run(fake, scorer, "t", max_messages=4)  # type: ignore[arg-type]
        assert counts["scored"] == 2 and counts["duplicate"] == 1 and counts["invalid"] == 1
        assert len(fake.committed) == 4 and fake.closed and len(scorer.store) == 2

    def test_no_commit_when_processing_fails(self, scorer: Scorer) -> None:
        def db_down(_record: Any) -> bool:
            raise ConnectionError("db down")

        scorer.store.save = db_down  # type: ignore[method-assign]
        fake = FakeConsumer([encode(event())[1]])
        with pytest.raises(ConnectionError):
            consumer_mod.run(fake, scorer, "t", max_messages=1)  # type: ignore[arg-type]
        assert fake.committed == [] and fake.closed

    def test_missing_topic_is_waited_out_not_fatal(self, scorer: Scorer) -> None:
        from confluent_kafka import KafkaError

        fake = FakeConsumer([])
        fake.queue = [
            FakeMessage(None, FakeError(KafkaError.UNKNOWN_TOPIC_OR_PART)),
            FakeMessage(encode(event())[1]),
        ]
        counts = consumer_mod.run(fake, scorer, "t", max_messages=1)  # type: ignore[arg-type]
        assert counts["scored"] == 1 and len(fake.committed) == 1

    def test_other_broker_errors_are_fatal(self, scorer: Scorer) -> None:
        from confluent_kafka import KafkaError, KafkaException

        fake = FakeConsumer([])
        fake.queue = [FakeMessage(None, FakeError(KafkaError._TRANSPORT))]
        with pytest.raises(KafkaException):
            consumer_mod.run(fake, scorer, "t", max_messages=1)  # type: ignore[arg-type]
        assert fake.committed == [] and fake.closed

    def test_idle_timeout_stops(self, scorer: Scorer) -> None:
        counts = consumer_mod.run(FakeConsumer([]), scorer, "t", idle_timeout=0)  # type: ignore[arg-type]
        assert sum(counts.values()) == 0


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
