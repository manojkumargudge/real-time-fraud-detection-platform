"""Fraud-ring grouping (no torch needed): uid + device links only, hub devices dropped."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.graph.rings import (
    DEVICE,
    hub_uids,
    linking_devices,
    ring_components,
    size_stats,
    split_ranked,
    summarize_rings,
)


def links(rows: list[tuple[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["uid", DEVICE])


def test_users_sharing_a_device_form_a_ring_and_chains_merge() -> None:
    comp = ring_components(
        links([("u1", "d1"), ("u2", "d1"), ("u2", "d2"), ("u3", "d2"), ("u4", "d9")])
    )
    assert comp["u1"] == comp["u2"] == comp["u3"]  # u1-d1-u2-d2-u3
    assert "u4" not in comp.index  # a device used by one user links nobody


def test_hub_devices_do_not_link_users() -> None:
    rows = [(f"u{i}", "Windows") for i in range(11)] + [("a", "rare"), ("b", "rare")]
    comp = ring_components(links(rows), device_uid_limit=10)
    assert set(comp.index) == {"a", "b"}  # 11 users on "Windows" exceed the limit of 10


def test_card_id_is_not_a_link() -> None:
    """Two users on the same coarse card_id but no shared device are not a ring."""
    tx = pd.DataFrame(
        {
            "uid": ["u1", "u2", "u3", "u4"],
            "card_id": ["c", "c", "c", "x"],
            DEVICE: ["d1", "d2", "d3", "d3"],
            "TransactionAmt": [1.0, 2.0, 3.0, 4.0],
            "TransactionDT": [1, 2, 3, 4],
            "isFraud": [0, 0, 1, 0],
            "gnn_score": [0.1, 0.2, 0.9, 0.3],
        }
    )
    comp = ring_components(tx)
    assert set(comp.index) == {"u3", "u4"}  # linked by device d3, despite different cards
    rings = summarize_rings(tx, comp)
    (ring,) = rings.itertuples()
    assert (ring.uid_count, ring.card_count, ring.tx_count) == (2, 2, 2)
    assert ring.mean_risk == 0.6 and ring.fraud_rate == 0.5 and ring.shared_devices == ["d3"]


def test_rings_are_ranked_by_risk_not_by_labels() -> None:
    tx = pd.DataFrame(
        {
            "uid": ["a", "b", "c", "d"],
            "card_id": ["1", "2", "3", "4"],
            DEVICE: ["x", "x", "y", "y"],
            "TransactionAmt": 1.0,
            "TransactionDT": [1, 2, 3, 4],
            "isFraud": [1, 1, 0, 0],  # the labelled-fraud ring has the lower score
            "gnn_score": [0.1, 0.1, 0.8, 0.8],
        }
    )
    ranked, unscored = split_ranked(summarize_rings(tx, ring_components(tx)))
    assert ranked["mean_risk"].tolist() == [0.8, 0.1] and unscored.empty


def test_rings_without_any_score_are_listed_as_unscored_not_ranked_last() -> None:
    tx = pd.DataFrame(
        {
            "uid": ["a", "b", "c", "d", "e", "f"],
            "card_id": list("123456"),
            DEVICE: ["x", "x", "y", "y", "z", "z"],
            "TransactionAmt": 1.0,
            "TransactionDT": range(6),
            "isFraud": 0,
            "gnn_score": [None, None, 0.2, None, 0.5, 0.7],  # ring x: first block, no score
        }
    )
    rings = summarize_rings(tx, ring_components(tx))
    ranked, unscored = split_ranked(rings)
    assert len(unscored) == 1 and unscored["scored_tx"].tolist() == [0]
    assert ranked["mean_risk"].tolist() == [0.6, 0.2]  # partly scored ring y uses its 1 score
    assert ranked["scored_tx"].tolist() == [2, 1]
    stats = size_stats(rings)
    assert (stats["rings"], stats["largest"]) == (3, 2)
    assert stats["largest_share"] == 2 / 6


def test_hub_uids_with_many_devices_or_transactions_are_excluded() -> None:
    tx = pd.DataFrame(
        {
            "uid": ["busy"] * 4 + ["heavy"] * 3 + ["ok", "ok"],
            DEVICE: ["d1", "d2", "d3", "d4", "x", "x", "x", "d1", None],
        }
    )
    hubs = hub_uids(tx, device_limit=3, tx_limit=2)
    assert set(hubs) == {"busy", "heavy"}  # 4 devices > 3; 3 transactions > 2


def test_only_linking_devices_are_listed_as_shared() -> None:
    rows = [(f"h{i}", "Windows") for i in range(11)] + [("a", "rare"), ("b", "rare")]
    tx = pd.DataFrame(
        {
            "uid": [u for u, _ in rows] + ["a", "b"],
            "card_id": "c",
            DEVICE: [d for _, d in rows] + ["Windows", "Windows"],  # a and b also use Windows
            "TransactionAmt": 1.0,
            "TransactionDT": 0,
            "isFraud": 0,
            "gnn_score": 0.5,
        }
    )
    links = linking_devices(tx, 10)
    assert links == {"rare"}
    rings = summarize_rings(tx, ring_components(tx, 10), links)
    assert rings["shared_devices"].tolist() == [["rare"]]  # not Windows


def test_account_graph_never_uses_the_default_score_table() -> None:
    from src.graph.build import (
        GNN_ACCOUNT_SCORES_PATH,
        GNN_SCORES_PATH,
        checked_scores_path,
        report_path,
    )

    assert checked_scores_path("default", None) == GNN_SCORES_PATH
    assert checked_scores_path("account", None) == GNN_ACCOUNT_SCORES_PATH
    assert GNN_ACCOUNT_SCORES_PATH.parent.name == "gnn-account"
    with pytest.raises(SystemExit, match="must not use"):
        checked_scores_path("account", GNN_SCORES_PATH)
    assert report_path(Path("reports/x.md"), "account") == Path("reports/x_account.md")
    assert report_path(Path("reports/x.md"), "default") == Path("reports/x.md")


@pytest.mark.parametrize(
    "argv", [["--graph", "account"], ["--scores", "artifacts/gnn-account/scores.parquet"]]
)
def test_rings_refuse_a_db_write_with_non_default_scores(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.graph import rings

    monkeypatch.setenv("DATABASE_URL", "postgresql://blocked:blocked@127.0.0.1:1/blocked")
    monkeypatch.setattr(rings, "load_ieee_cis", lambda *a, **k: pytest.fail("loaded data"))
    with pytest.raises(SystemExit, match="refusing to write fraud_rings"):
        rings.main(argv)
