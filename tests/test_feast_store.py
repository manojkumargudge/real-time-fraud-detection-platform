"""Feast: point-in-time (offline) retrieval of the history features equals the batch features."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("feast")

from src.data.load import DEFAULT_RAW_DIR, load_ieee_cis  # noqa: E402
from src.features import feast_store as fs  # noqa: E402
from src.features.entity import CARD_ID, USER_ID, build_entity_keys  # noqa: E402
from src.features.history import HistoryConfig, compute_history_features  # noqa: E402
from src.serving.online_features import events_to_frame  # noqa: E402
from tests.test_entity_state import keys_by_id, make_events  # noqa: E402

CONFIG = HistoryConfig()
NAMES = CONFIG.feature_names()


def entity_events(events: list[dict[str, Any]]) -> list[fs.EntityEvent]:
    keys = keys_by_id(events)
    return [fs.EntityEvent(e, *keys[e["TransactionID"]]) for e in events]


def historical_features(events: list[dict[str, Any]], repo: Path) -> pd.DataFrame:
    """Feast point-in-time features for every event, at the event's own timestamp."""
    ents = entity_events(events)
    uid_rows, card_rows = fs.fold_states(ents, CONFIG, snapshots=True)
    fs.write_offline(repo, uid_rows, card_rows, config=CONFIG)
    store = fs.open_store(repo, config=CONFIG)
    entity_df = pd.DataFrame(
        [
            fs.request_row(e.event, e.card, e.uid, CONFIG.change_columns)
            | {
                fs.EVENT_TIMESTAMP: fs.to_datetime(e.event["TransactionDT"]),
                "TransactionID": e.event["TransactionID"],
            }
            for e in ents
        ]
    )
    return fs.historical_features(store, entity_df, CONFIG)


def batch_features(events: list[dict[str, Any]]) -> pd.DataFrame:
    frame = events_to_frame(events)
    return compute_history_features(frame, CONFIG).set_index(frame["TransactionID"])


def assert_same(feast_df: pd.DataFrame, batch: pd.DataFrame) -> None:
    assert len(feast_df) == len(batch) and feast_df.index.is_unique
    got = feast_df.loc[batch.index, NAMES].astype("float64").astype("float32").to_numpy(np.float64)
    np.testing.assert_allclose(got, batch[NAMES].to_numpy(np.float64), rtol=1e-6, equal_nan=True)


def test_point_in_time_retrieval_equals_batch_features(tmp_path: Path) -> None:
    events = make_events(250, 11)  # same-timestamp events, missing keys, device/address changes
    keys = keys_by_id(events)
    stamps = pd.DataFrame([(*keys[e["TransactionID"]], e["TransactionDT"]) for e in events])
    assert stamps.duplicated().any()  # rows Feast would collapse without historical_features
    assert_same(historical_features(events, tmp_path), batch_features(events))


@pytest.mark.skipif(
    not (DEFAULT_RAW_DIR / "train_transaction.csv").is_file(),
    reason="IEEE-CIS dataset not downloaded",
)
def test_point_in_time_retrieval_equals_batch_on_real_cards(tmp_path: Path) -> None:
    """All events of a sample of real cards (each card's full history, so features are exact)."""
    df = load_ieee_cis(DEFAULT_RAW_DIR, usecols=[])
    cards = build_entity_keys(df)[CARD_ID]
    counts = cards.value_counts()
    sample = counts[(counts >= 5) & (counts <= 60)].sample(40, random_state=0).index
    rows = df[cards.isin(sample)].head(1_500)
    from src.serving.online_features import EVENT_FIELDS

    events = [
        {str(k): (None if pd.isna(v) else v) for k, v in r.items()}
        for r in rows[list(EVENT_FIELDS)].to_dict(orient="records")
    ]
    feast_df = historical_features(events, tmp_path)
    batch = batch_features(events)
    assert batch[USER_ID].notna().all()
    assert_same(feast_df, batch)


def test_replay_store_and_feast_paths_give_same_features_and_scores(
    bundle_dir: Path, tmp_path: Path
) -> None:
    """In-order events (shuffled within each timestamp) through both serving paths."""
    from src.serving.model import ModelBundle
    from src.serving.schema import TransactionEvent
    from src.serving.scorer import Scorer
    from src.serving.store import InMemoryStore
    from tests.test_entity_state import arrival_order

    bundle = ModelBundle.load(bundle_dir)
    feast = fs.FeastFeatures(
        fs.open_store(tmp_path, config=bundle.history_config), bundle.history_config
    )
    by_store = Scorer(bundle, InMemoryStore())
    by_feast = Scorer(bundle, InMemoryStore(), feast=feast, read_from_feast=True)
    events = arrival_order(make_events(160, 5), 5)
    for ev in events:
        a = by_store.score_event(TransactionEvent.model_validate(ev))
        b = by_feast.score_event(TransactionEvent.model_validate(ev))
        assert a.history_features.keys() == b.history_features.keys()
        for name, va in a.history_features.items():
            vb = b.history_features[name]
            assert (va is None) == (vb is None), (ev["TransactionID"], name, va, vb)
            if va is not None and vb is not None:
                assert vb == pytest.approx(va, rel=1e-6), (ev["TransactionID"], name)
        assert b.score == pytest.approx(a.score, abs=1e-6), ev["TransactionID"]
        assert b.is_alert == a.is_alert
    keyless = [e for e in events if e["card1"] is None]
    assert keyless  # events with missing entity keys were scored on both paths


def test_open_store_writes_only_inside_its_repo_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd, repo = tmp_path / "cwd", tmp_path / "repo"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    fs.open_store(repo, config=CONFIG)
    assert list(cwd.iterdir()) == []  # Feast resolves the registry against the cwd by default
    assert (repo / "registry.db").is_file()
