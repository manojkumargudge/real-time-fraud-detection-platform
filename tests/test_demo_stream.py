"""Demo defaults: Feast is the default feature source; the producer streams validation data only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.data.load import DEFAULT_RAW_DIR, load_transactions
from src.features.split import time_cutoff, validation_start
from src.serving.config import Settings
from src.streaming.producer import StreamOrderError, check_order, stream_events


def test_feast_is_the_default_feature_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEATURE_SOURCE")  # the conftest guard sets "store" for unit tests
    assert Settings.from_env().feature_source == "feast"


def test_feast_without_redis_fails_at_scorer_build_not_at_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FEATURE_SOURCE")
    settings = Settings.from_env()  # the producer only needs Kafka settings: must not raise
    with pytest.raises(ValueError, match="FEAST_REDIS"):
        settings.build_scorer()


def test_unknown_feature_source_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_SOURCE", "cache")
    with pytest.raises(ValueError, match="FEATURE_SOURCE"):
        Settings.from_env()


def test_check_order_refuses_events_before_the_cutoff_or_out_of_order() -> None:
    ok = [{"TransactionDT": 100}, {"TransactionDT": 100}, {"TransactionDT": 150}]
    assert list(check_order(ok, not_before=100)) == ok
    with pytest.raises(StreamOrderError, match="before the stream cut-off"):
        list(check_order([{"TransactionDT": 99}], not_before=100))
    with pytest.raises(StreamOrderError, match="out of order"):
        list(check_order([{"TransactionDT": 150}, {"TransactionDT": 120}], not_before=100))


def test_validation_start_is_the_milestone_2_cutoff() -> None:
    import pandas as pd

    dt = pd.Series(range(1, 101))
    assert validation_start(dt) == time_cutoff(dt, 0.2) == 81


@pytest.mark.skipif(
    not (DEFAULT_RAW_DIR / "train_transaction.csv").is_file(),
    reason="IEEE-CIS dataset not downloaded",
)
def test_demo_stream_starts_at_the_validation_cutoff_in_time_order() -> None:
    start, events = stream_events(str(DEFAULT_RAW_DIR), 0, 500)
    dt = load_transactions(DEFAULT_RAW_DIR / "train_transaction.csv", usecols=["TransactionDT"])
    assert start == validation_start(dt["TransactionDT"]) == 12_192_900
    times = [e["TransactionDT"] for e in events]
    assert times[0] >= start and times == sorted(times)
    assert all("isFraud" not in e for e in events)  # labels are never sent


def test_feast_push_happens_after_save_and_never_for_duplicates(bundle_dir: Path) -> None:
    """Order: dedupe -> save -> push. A duplicate (redelivery) must not be applied twice."""
    from src.serving.model import ModelBundle
    from src.serving.schema import TransactionEvent
    from src.serving.scorer import Scorer
    from src.serving.store import InMemoryStore
    from tests.test_entity_state import make_events

    store = InMemoryStore()
    calls: list[tuple[str, int, bool]] = []

    class RecordingFeast:
        def read(
            self, ev: dict[str, Any], card: Any, uid: Any
        ) -> tuple[dict[str, float], dict[str, Any]]:
            return {}, {}

        def push(self, ev: dict[str, Any], card: Any, uid: Any, online: dict[str, Any]) -> None:
            calls.append(("push", ev["TransactionID"], store.get(ev["TransactionID"]) is not None))

    scorer = Scorer(ModelBundle.load(bundle_dir), store, feast=RecordingFeast())  # type: ignore[arg-type]
    event = TransactionEvent.model_validate(make_events(1, 0)[0] | {"card1": 1111})
    first, again = scorer.score_event(event), scorer.score_event(event)
    assert not first.duplicate and again.duplicate
    assert calls == [("push", event.TransactionID, True)]  # once, and only after the save
