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


def stream_scenario() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Batch history, then a stream. Card 2222 has only pre-stream history, 3333 appears only
    in the stream, 1111 spans both; plus missing keys, ties and device/address changes."""
    pre = make_events(140, 21, cards=(1111, 2222, None))
    stream = make_events(
        150, 22, cards=(1111, 3333, None), first_id=50_000, start=pre[-1]["TransactionDT"] + 1
    )
    return pre, stream


@pytest.mark.parametrize("use_odfv", [False, True], ids=["local", "odfv"])
def test_stream_replay_with_batch_cold_start(
    bundle_dir: Path, tmp_path: Path, use_odfv: bool
) -> None:
    """Feast starts from the batch (pre-stream) state; the store path only knows the stream.

    * cards with pre-stream history: Feast features == batch (training) features;
    * cards without: store path == Feast path (features and scores);
    * the store path disagrees with batch on cards with pre-stream history (the skew Feast fixes).
    """
    from src.serving.model import ModelBundle
    from src.serving.schema import TransactionEvent
    from src.serving.scorer import Scorer
    from src.serving.store import InMemoryStore
    from tests.test_entity_state import arrival_order

    bundle = ModelBundle.load(bundle_dir)
    cfg = bundle.history_config
    pre, stream = stream_scenario()
    store = fs.open_store(tmp_path, config=cfg)
    fs.materialize_initial_state(store, tmp_path, entity_events(pre), cfg)
    feast = fs.FeastFeatures(store, cfg, use_odfv=use_odfv)
    by_store = Scorer(bundle, InMemoryStore())
    by_feast = Scorer(bundle, InMemoryStore(), feast=feast, read_from_feast=True)
    batch = batch_features(pre + stream)
    pre_cards = {k[0] for k in keys_by_id(pre).values() if k[0] is not None}

    store_vs_batch_diffs, compared = 0, {"with_history": 0, "without_history": 0}
    for ev in arrival_order(stream, 22):
        a = by_store.score_event(TransactionEvent.model_validate(ev))
        b = by_feast.score_event(TransactionEvent.model_validate(ev))
        card = batch.loc[ev["TransactionID"], CARD_ID]
        expected = batch.loc[ev["TransactionID"], NAMES].to_numpy(np.float64)
        got_feast = np.array([np.nan if v is None else v for v in b.history_features.values()])
        got_store = np.array([np.nan if v is None else v for v in a.history_features.values()])
        np.testing.assert_allclose(got_feast, expected, rtol=1e-6, equal_nan=True)  # Feast == batch
        if isinstance(card, str) and card in pre_cards:
            compared["with_history"] += 1
            store_vs_batch_diffs += not np.allclose(got_store, expected, rtol=1e-6, equal_nan=True)
        else:
            compared["without_history"] += 1
            np.testing.assert_allclose(got_store, got_feast, rtol=1e-6, equal_nan=True)
            assert b.score == pytest.approx(a.score, abs=1e-6), ev["TransactionID"]
    assert compared["with_history"] > 0 and compared["without_history"] > 0, compared
    assert store_vs_batch_diffs > 0  # without batch history the store path is skewed


def test_open_store_writes_only_inside_its_repo_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd, repo = tmp_path / "cwd", tmp_path / "repo"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    fs.open_store(repo, config=CONFIG)
    assert list(cwd.iterdir()) == []  # Feast resolves the registry against the cwd by default
    assert (repo / "registry.db").is_file()


def test_pre_stream_events_are_strictly_earlier_with_batch_keys() -> None:
    events = make_events(120, 31)
    frame = events_to_frame(events)
    before = int(frame["TransactionDT"].iloc[60])
    pre = fs.pre_stream_events(frame, before, CONFIG)
    assert pre and all(e.event["TransactionDT"] < before for e in pre)
    assert len(pre) == int((frame["TransactionDT"] < before).sum())  # ties at `before` excluded
    keys = keys_by_id(events)
    assert all((e.card, e.uid) == keys[e.event["TransactionID"]] for e in pre)
