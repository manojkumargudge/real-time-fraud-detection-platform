"""GNN leakage rules (needs requirements-gnn.txt; skipped where torch_geometric is missing)."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("torch_geometric")
pytest.importorskip("pyg_lib")

import torch  # noqa: E402

from src.data.load import LABEL  # noqa: E402
from src.features.entity import CARD_ID, USER_ID, build_entity_keys  # noqa: E402
from src.graph.build import (  # noqa: E402
    ACCOUNT,
    ACCOUNT_CARD,
    ENTITY_COLUMNS,
    NO_LABEL,
    build_graph,
    entity_frame,
)
from src.graph.data import TX, temporal_loader, to_hetero  # noqa: E402
from src.graph.train import GnnConfig, predict, train_model  # noqa: E402

NO_HUBS = 10**9  # keep every entity value, so 2-hop paths through device/email/addr exist
TINY = GnnConfig(hidden=8, max_epochs=1, max_batches_per_epoch=3, batch_size=256)


def test_temporal_subgraphs_are_strictly_earlier_per_seed(synthetic_frame: pd.DataFrame) -> None:
    """Every tx in a seed's subgraph, other than the seed, has TransactionDT < the seed's --
    checked per seed with the disjoint batch assignment, including 2-hop paths via entities."""
    graph, _ = build_graph(synthetic_frame, include_validation=True, degree_limit=NO_HUBS)
    data = to_hetero(graph)
    time = data[TX].time
    seeds = np.arange(graph.num_tx, dtype=np.int64)
    checked, reached_via = 0, dict.fromkeys(ENTITY_COLUMNS, 0)
    for batch in temporal_loader(data, seeds, batch_size=200, fanout=(-1, -1)):
        k = batch[TX].batch_size
        n_id, owner = batch[TX].n_id, batch[TX].batch  # owner = index of the node's seed
        seed_time = time[n_id[:k]][owner]
        is_seed = torch.arange(len(n_id)) < k
        assert bool((time[n_id][~is_seed] < seed_time[~is_seed]).all())
        assert bool((owner[:k] == torch.arange(k)).all())  # seeds come first, one per subgraph
        checked += int((~is_seed).sum())
        for t in ENTITY_COLUMNS:  # second hop: entity -> earlier tx
            reached_via[t] += int(batch[t, f"rev_{t}", TX].edge_index.shape[1])
    assert checked > 1_000  # many non-seed transactions were actually sampled
    assert all(v > 0 for v in reached_via.values()), reached_via


def test_same_timestamp_transactions_are_not_sampled(synthetic_frame: pd.DataFrame) -> None:
    df = synthetic_frame.copy()
    df.loc[df.index[:400], "TransactionDT"] = int(df["TransactionDT"].iloc[0])  # one tied block
    graph, _ = build_graph(
        df.sort_values("TransactionDT"), include_validation=True, degree_limit=NO_HUBS
    )
    data = to_hetero(graph)
    tied = np.flatnonzero(graph.tx_time == graph.tx_time.min()).astype(np.int64)
    for batch in temporal_loader(data, tied, batch_size=100, fanout=(-1, -1)):
        k = batch[TX].batch_size
        assert len(batch[TX].n_id) == k  # nothing earlier exists, ties must not be sampled


def test_training_graph_and_subgraphs_never_contain_validation(
    synthetic_frame: pd.DataFrame,
) -> None:
    train_graph, labels = build_graph(
        synthetic_frame, include_validation=False, degree_limit=NO_HUBS
    )
    full_graph, _ = build_graph(synthetic_frame, include_validation=True, degree_limit=NO_HUBS)
    cutoff = train_graph.split.cutoff
    assert labels is None and not train_graph.is_valid.any()
    assert (train_graph.tx_time < cutoff).all()
    valid_ids = set(full_graph.tx_id[full_graph.is_valid].tolist())
    assert valid_ids and not valid_ids & set(train_graph.tx_id.tolist())
    data = to_hetero(train_graph)
    seeds = np.flatnonzero(train_graph.is_fit | train_graph.is_holdout).astype(np.int64)
    for batch in temporal_loader(data, seeds, batch_size=256, fanout=(-1, -1)):
        ids = set(train_graph.tx_id[batch[TX].n_id.numpy()].tolist())
        assert not ids & valid_ids


def test_validation_labels_never_reach_the_graph(synthetic_frame: pd.DataFrame) -> None:
    graph, valid_labels = build_graph(
        synthetic_frame, include_validation=True, degree_limit=NO_HUBS
    )
    assert valid_labels is not None and len(valid_labels) == int(graph.is_valid.sum())
    assert (graph.labels[graph.is_valid] == NO_LABEL).all()
    assert set(graph.labels[~graph.is_valid].tolist()) <= {0, 1}
    forbidden = {LABEL, "D1", "TransactionDT", "TransactionID", "card_id", "uid"}
    assert not forbidden & set(graph.numeric_columns + graph.categorical_columns)


def test_fit_holdout_validation_are_time_ordered(synthetic_frame: pd.DataFrame) -> None:
    graph, _ = build_graph(synthetic_frame, include_validation=True, degree_limit=NO_HUBS)
    s, t = graph.split, graph.tx_time
    assert s.holdout_start < s.cutoff
    assert (t[graph.is_fit] < s.holdout_start).all()
    assert ((t[graph.is_holdout] >= s.holdout_start) & (t[graph.is_holdout] < s.cutoff)).all()
    assert (t[graph.is_valid] >= s.cutoff).all()
    assert (graph.is_fit.astype(int) + graph.is_holdout + graph.is_valid == 1).all()


def test_changing_validation_labels_changes_nothing_in_training(
    synthetic_frame: pd.DataFrame,
) -> None:
    """Flip every validation label: the trained model and its scores must be identical."""
    flipped = synthetic_frame.copy()
    cutoff = build_graph(synthetic_frame, include_validation=False, degree_limit=NO_HUBS)[
        0
    ].split.cutoff
    valid = flipped["TransactionDT"] >= cutoff
    flipped.loc[valid, LABEL] = 1 - flipped.loc[valid, LABEL]
    runs = []
    for df in (synthetic_frame, flipped):
        train_graph, _ = build_graph(df, include_validation=False, degree_limit=NO_HUBS)
        full_graph, _ = build_graph(df, include_validation=True, degree_limit=NO_HUBS)
        result = train_model(train_graph, TINY)
        seeds = np.flatnonzero(full_graph.is_valid).astype(np.int64)
        runs.append(
            (
                result.holdout_pr_auc,
                result.threshold,
                predict(result.model, to_hetero(full_graph), seeds, TINY),
            )
        )
    assert runs[0][0] == runs[1][0] and runs[0][1] == runs[1][1]
    np.testing.assert_array_equal(runs[0][2], runs[1][2])


def test_train_model_refuses_a_graph_with_validation_rows(synthetic_frame: pd.DataFrame) -> None:
    full_graph, _ = build_graph(synthetic_frame, include_validation=True, degree_limit=NO_HUBS)
    with pytest.raises(ValueError, match="training graph"):
        train_model(full_graph, TINY)


def test_hubs_are_counted_on_training_rows_only_and_dropped(synthetic_frame: pd.DataFrame) -> None:
    limit = 300
    graph, _ = build_graph(synthetic_frame, include_validation=True, degree_limit=limit)
    entities = entity_frame(synthetic_frame)
    train = synthetic_frame["TransactionDT"] < graph.split.cutoff
    for t in ENTITY_COLUMNS:
        counts = entities.loc[train, t].dropna().value_counts()
        assert {v for v, _ in graph.hubs[t]} == set(counts[counts > limit].index)
        assert not {v for v, _ in graph.hubs[t]} & set(graph.entity_names[t])  # no hub nodes
    assert any(graph.hubs.values())  # the synthetic emails/devices exceed the limit
    # Validation rows sharing a rare training value must not turn it into a hub.
    rare = entities.loc[train, "device"].value_counts().idxmin()
    boosted = synthetic_frame.copy()
    boosted.loc[~train, "DeviceInfo"] = rare
    hubs_after = build_graph(boosted, include_validation=True, degree_limit=limit)[0].hubs
    assert hubs_after["device"] == graph.hubs["device"]


def test_fixed_seed_training_is_reproducible(synthetic_frame: pd.DataFrame) -> None:
    graph, _ = build_graph(synthetic_frame, include_validation=False, degree_limit=NO_HUBS)
    a, b = train_model(graph, TINY), train_model(graph, TINY)
    seeds = np.flatnonzero(graph.is_holdout).astype(np.int64)
    np.testing.assert_array_equal(
        predict(a.model, to_hetero(graph), seeds, TINY),
        predict(b.model, to_hetero(graph), seeds, TINY),
    )
    assert replace(TINY, seed=7) != TINY


def test_forward_chaining_scores_each_block_only_from_earlier_blocks(
    synthetic_frame: pd.DataFrame,
) -> None:
    """No block is scored by a model trained on its own or later blocks; block 0 has no score."""
    from src.graph.stack import crossfit_scores, forward_folds, time_blocks

    graph, _ = build_graph(synthetic_frame, include_validation=False, degree_limit=NO_HUBS)
    block = time_blocks(graph.tx_time, 3)
    assert set(block.tolist()) == {0, 1, 2}
    assert (np.diff(block[np.argsort(graph.tx_time, kind="stable")]) >= 0).all()  # contiguous
    for k, train_mask in forward_folds(block, 3):
        assert train_mask.any() and (block[train_mask] < k).all()
        assert graph.tx_time[train_mask].max() < graph.tx_time[block == k].min()
    calls: list[np.ndarray[Any, Any]] = []

    def recording_trainer(g: object, cfg: GnnConfig, *, train_mask: np.ndarray[Any, Any]) -> object:
        calls.append(train_mask.copy())
        return train_model(graph, cfg, train_mask=train_mask)

    scores = crossfit_scores(graph, TINY, folds=3, trainer=recording_trainer)
    assert len(calls) == 2  # blocks 1 and 2 only
    assert np.isnan(scores[block == 0]).all() and np.isfinite(scores[block > 0]).all()
    for mask, k in zip(calls, (1, 2), strict=True):
        assert not (mask & (block >= k)).any()


def test_log_mlflow_writes_git_tags_that_callers_cannot_override(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mlflow.tracking import MlflowClient

    from src.graph.train import log_mlflow
    from src.models import tracking

    uri = f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", uri)
    git = {("rev-parse", "HEAD"): "abc123", ("status", "--porcelain"): " M src/graph/x.py"}
    monkeypatch.setattr(tracking, "_git", lambda *args: git[args])
    client = MlflowClient(tracking_uri=uri)

    caller = {"stage": "t", "git_commit": "fake", "git_dirty": "false"}
    dirty = client.get_run(log_mlflow(TINY, {"m": 1.0}, caller)).data.tags
    assert (dirty["git_commit"], dirty["git_dirty"], dirty["stage"]) == ("abc123", "true", "t")
    git[("status", "--porcelain")] = ""
    clean = client.get_run(log_mlflow(TINY, {"m": 1.0}, {"stage": "t"})).data.tags
    assert clean["git_dirty"] == "false"


def edge_fingerprint(data: Any) -> dict[str, tuple[int, str]]:
    """``"src__rel__dst" -> (edge count, sha256 of edge_index)`` per edge type."""
    return {
        "__".join(et): (
            int(data[et].edge_index.shape[1]),
            hashlib.sha256(np.ascontiguousarray(data[et].edge_index.numpy()).tobytes()).hexdigest(),
        )
        for et in data.edge_types
    }


# Recorded from main d8241d7 (before the account node) on the synthetic frame,
# include_validation=True, no hubs. The same check on the real data: docs/gnn.md "Account node".
DEFAULT_GRAPH_MAIN: dict[str, Any] = {
    "nodes": {"tx": 3000, "card": 594, "device": 3, "p_email": 2, "r_email": 1, "addr1": 2},
    "edges": {
        "tx__has_card__card": (
            3000,
            "aba2e4995ad649e54e698af8a3161c679c382002e52592b38e82bd7b58403274",
        ),
        "card__rev_card__tx": (
            3000,
            "ed6ee2f291ba720689b75e97e35a33be2b1f87347ef537483f2b41c7e74e10fe",
        ),
        "tx__has_device__device": (
            952,
            "326055cb83ac592bf83578eb6d77a296a1806bcb90b5ffd1cdcd27f3a5959f1c",
        ),
        "device__rev_device__tx": (
            952,
            "ad65357ee19987bc53f24c13a935aa938bc8a72fa93eac5f814bd2a1b7a481cb",
        ),
        "tx__has_p_email__p_email": (
            2025,
            "2ddf5f490a2eb99072f09be058aec555280e062bc16c8f0176f9ecd4c9564bb8",
        ),
        "p_email__rev_p_email__tx": (
            2025,
            "221595a32ec0823cd32f6408a0ce8d135de119c7c19eb6e3e5d6a1e14addad5b",
        ),
        "tx__has_r_email__r_email": (
            1511,
            "f355ffb78456b0581bf392c01ac2881e1248132f464b14bbf130b8e168abff14",
        ),
        "r_email__rev_r_email__tx": (
            1511,
            "4ce566cda0e1a55e37bd1a2b42d76fd418a3a6e77b4c92debd61043fff7dc6f8",
        ),
        "tx__has_addr1__addr1": (
            1967,
            "351e1b689fb62309d8c6fe4b307b24275cc9140f3fe3e6a0b35b287fdc4c08a4",
        ),
        "addr1__rev_addr1__tx": (
            1967,
            "fa07176bf1238087f7406579f9ba781c9a354c765aee534d56ba0631732842dd",
        ),
    },
}


def test_default_graph_is_identical_to_the_graph_before_the_account_node(
    synthetic_frame: pd.DataFrame,
) -> None:
    graph, _ = build_graph(synthetic_frame, include_validation=True, degree_limit=NO_HUBS)
    data = to_hetero(graph)
    nodes = {t: int(data[t].num_nodes) for t in data.node_types}
    print("default graph nodes", nodes)
    assert nodes == DEFAULT_GRAPH_MAIN["nodes"]
    assert edge_fingerprint(data) == DEFAULT_GRAPH_MAIN["edges"]
    assert ACCOUNT not in data.node_types and graph.entity_edges == {}
    explicit, _ = build_graph(
        synthetic_frame, include_validation=True, degree_limit=NO_HUBS, account=False
    )
    assert edge_fingerprint(to_hetero(explicit)) == DEFAULT_GRAPH_MAIN["edges"]


def test_account_graph_adds_account_nodes_and_edges_only(synthetic_frame: pd.DataFrame) -> None:
    graph, _ = build_graph(
        synthetic_frame, include_validation=True, degree_limit=NO_HUBS, account=True
    )
    data = to_hetero(graph)
    keys = build_entity_keys(synthetic_frame)
    has_addr = synthetic_frame["addr1"].notna()
    expected_accounts = set(keys.loc[has_addr, USER_ID])
    assert 0 < has_addr.sum() < len(synthetic_frame)  # rows with and without an address
    print("accounts", len(expected_accounts), "tx-account edges", int(has_addr.sum()))

    assert set(graph.entity_names[ACCOUNT]) == expected_accounts
    assert not any(n.endswith("<NA>") for n in graph.entity_names[ACCOUNT])  # no <NA> uids
    tx_account = data[TX, "has_account", ACCOUNT].edge_index
    assert tx_account.shape[1] == int(has_addr.sum())  # one per transaction with an address
    assert set(tx_account[0].tolist()) == set(np.flatnonzero(has_addr.to_numpy()).tolist())
    # Each account links to exactly its own card (the uid contains the card).
    acc_card = data[ACCOUNT_CARD].edge_index
    assert acc_card.shape[1] == len(expected_accounts)
    names, cards = graph.entity_names[ACCOUNT], graph.entity_names["card"]
    card_of = dict(zip(keys[USER_ID], keys[CARD_ID], strict=True))
    for a, c in acc_card.T.tolist():
        assert cards[c] == card_of[names[a]]
    assert data["card", "rev_uses_card", ACCOUNT].edge_index.shape[1] == acc_card.shape[1]
    # Every other node and edge type is exactly the default graph's.
    other = {k: v for k, v in edge_fingerprint(data).items() if ACCOUNT not in k}
    assert other == DEFAULT_GRAPH_MAIN["edges"]
    for t, n in DEFAULT_GRAPH_MAIN["nodes"].items():
        assert int(data[t].num_nodes) == n


def test_account_hubs_and_hub_cards_are_dropped_like_other_entities(
    synthetic_frame: pd.DataFrame,
) -> None:
    limit = 10
    graph, _ = build_graph(
        synthetic_frame, include_validation=True, degree_limit=limit, account=True
    )
    entities = entity_frame(synthetic_frame, account=True)
    train = synthetic_frame["TransactionDT"] < graph.split.cutoff
    counts = entities.loc[train, ACCOUNT].dropna().value_counts()
    hubs = {v for v, _ in graph.hubs[ACCOUNT]}
    assert hubs == set(counts[counts > limit].index)  # training degree, as for the others
    assert not hubs & set(graph.entity_names[ACCOUNT])
    accounts, cards = graph.entity_names[ACCOUNT], graph.entity_names["card"]
    card_hubs = {v for v, _ in graph.hubs["card"]}
    assert card_hubs  # the limit makes some cards hubs
    for a, c in graph.entity_edges[ACCOUNT_CARD].T.tolist():
        assert accounts[a] not in hubs and cards[c] not in card_hubs
    # An account whose card is a hub keeps its tx edges but has no account -> card edge.
    linked = {accounts[a] for a in graph.entity_edges[ACCOUNT_CARD][0].tolist()}
    first = entities.dropna(subset=[ACCOUNT]).drop_duplicates(ACCOUNT)
    card_of = dict(zip(first[ACCOUNT], first["card"], strict=True))
    for a in set(accounts) - linked:
        assert card_of[a] in card_hubs


def test_account_graph_subgraphs_are_strictly_earlier_and_use_account_paths(
    synthetic_frame: pd.DataFrame,
) -> None:
    graph, _ = build_graph(
        synthetic_frame, include_validation=True, degree_limit=NO_HUBS, account=True
    )
    data = to_hetero(graph)
    time = data[TX].time
    seeds = np.arange(graph.num_tx, dtype=np.int64)
    via_account, via_card = 0, 0
    for batch in temporal_loader(data, seeds, batch_size=300, fanout=(-1, -1)):
        k = batch[TX].batch_size
        n_id, owner = batch[TX].n_id, batch[TX].batch
        is_seed = torch.arange(len(n_id)) < k
        assert bool((time[n_id][~is_seed] < time[n_id[:k]][owner][~is_seed]).all())
        via_account += int(batch[ACCOUNT, "rev_account", TX].edge_index.shape[1])
        via_card += int(batch[ACCOUNT_CARD].edge_index.shape[1])
    assert via_account > 0 and via_card > 0


def test_account_graph_trains_and_predicts(synthetic_frame: pd.DataFrame) -> None:
    graph, _ = build_graph(
        synthetic_frame, include_validation=False, degree_limit=NO_HUBS, account=True
    )
    result = train_model(graph, TINY)
    seeds = np.flatnonzero(graph.is_holdout).astype(np.int64)
    scores = predict(result.model, to_hetero(graph), seeds, TINY)
    assert scores.shape == (len(seeds),) and np.isfinite(scores).all()
