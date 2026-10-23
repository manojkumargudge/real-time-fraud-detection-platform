"""Candidate fraud rings: users (uid = card + addr1) linked through shared, non-hub devices.

Usage: ``python -m src.graph.rings`` after ``src.graph.stack`` (reads the GNN scores in
``artifacts/gnn/scores.parquet``). Writes reports/fraud_rings.md and, with ``DATABASE_URL``,
replaces the rows of table ``fraud_rings`` (created if missing) for the dashboard.

Rings are connected components of the bipartite graph uid -- ``DeviceInfo``:

* ``card_id`` is **not** a link: it is a coarse proxy (77 values cover 42% of training
  transactions), so linking on it would create fake rings. uid already contains the card.
* Hub devices are dropped: a ``DeviceInfo`` value links uids only if it is shared by at most
  ``DEVICE_UID_LIMIT`` distinct uids (``Windows`` is shared by 10,896).
* Hub uids are dropped too: uid is itself a proxy, and a uid with many device values or
  transactions (up to 609 devices / 9,900 transactions) bridges unrelated device groups into
  one blob. A uid takes part only with at most ``UID_DEVICE_LIMIT`` distinct devices and
  ``UID_TX_LIMIT`` transactions. With device hubs alone the largest ring held 84% of ring users.
* Rings are ranked by the mean GNN score of their *scored* transactions. Transactions in the
  first training time block have no score by design (forward chaining); a ring with no scored
  transaction at all is listed separately as **unscored**, never ranked. The labelled fraud
  rate is shown for evaluation only; it is never used to build or rank rings.

Limitation: in this anonymized dataset ``DeviceInfo`` is a device model / OS string, not a
device identifier, so shared values are weak evidence of a real ring (docs/gnn.md).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from src.data.load import DEFAULT_RAW_DIR, KEY, LABEL, load_ieee_cis
from src.features.entity import CARD_ID, USER_ID, build_entity_keys
from src.graph.build import GNN_SCORE as FEATURE
from src.graph.build import GNN_SCORES_PATH as SCORES

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / "reports" / "fraud_rings.md"
DEVICE = "DeviceInfo"
DEVICE_UID_LIMIT = 10
UID_DEVICE_LIMIT = 3
UID_TX_LIMIT = 1_000
TOP = 20
TABLE_DDL = """
CREATE TABLE IF NOT EXISTS fraud_rings (
    rank            integer PRIMARY KEY,
    ring_id         integer NOT NULL,
    uid_count       integer NOT NULL,
    card_count      integer NOT NULL,
    tx_count        integer NOT NULL,
    total_amount    double precision NOT NULL,
    shared_devices  jsonb NOT NULL,
    mean_risk       double precision NOT NULL,  -- mean over scored transactions
    scored_tx       integer NOT NULL,
    fraud_rate      double precision NOT NULL,  -- evaluation only, never used for ranking
    first_dt        bigint NOT NULL,
    last_dt         bigint NOT NULL,
    uids            jsonb NOT NULL,
    generated_at    timestamptz NOT NULL DEFAULT now()
)
"""


def _find(parent: np.ndarray[Any, Any], i: int) -> int:
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = int(parent[i])
    return i


def hub_uids(
    tx: pd.DataFrame, device_limit: int = UID_DEVICE_LIMIT, tx_limit: int = UID_TX_LIMIT
) -> pd.Index[Any]:
    """uids with more than ``device_limit`` distinct devices or ``tx_limit`` transactions."""
    devices = tx.dropna(subset=[DEVICE]).groupby(USER_ID, observed=True)[DEVICE].nunique()
    count = tx.groupby(USER_ID, observed=True).size()
    over = (devices.reindex(count.index, fill_value=0) > device_limit) | (count > tx_limit)
    hubs: pd.Index[Any] = over.index[over.to_numpy()]
    return hubs


def linking_devices(links: pd.DataFrame, device_uid_limit: int = DEVICE_UID_LIMIT) -> set[str]:
    """Device values that link users: shared by 2..``device_uid_limit`` distinct uids."""
    pairs = links[[USER_ID, DEVICE]].drop_duplicates()
    per_device = pairs.groupby(DEVICE, observed=True)[USER_ID].nunique()
    return {str(d) for d in per_device[(per_device >= 2) & (per_device <= device_uid_limit)].index}


def ring_components(
    links: pd.DataFrame, device_uid_limit: int = DEVICE_UID_LIMIT
) -> pd.Series[int]:
    """Component id per uid (only uids sharing a non-hub device with another uid).

    ``links``: one row per (uid, device) pair. Devices shared by 2..limit uids are kept."""
    pairs = links[[USER_ID, DEVICE]].drop_duplicates()
    pairs = pairs[pairs[DEVICE].astype(str).isin(linking_devices(links, device_uid_limit))]
    uids = pd.Index(pairs[USER_ID].unique())
    position = {u: i for i, u in enumerate(uids)}
    parent = np.arange(len(uids))
    for _, group in pairs.groupby(DEVICE, observed=True):
        idx = [position[u] for u in group[USER_ID]]
        root = _find(parent, int(idx[0]))
        for j in idx[1:]:
            r = _find(parent, int(j))
            if r != root:
                parent[r] = root
    comp = np.array([_find(parent, i) for i in range(len(uids))])
    return pd.Series(pd.factorize(comp)[0], index=uids, name="ring_id")


def summarize_rings(
    tx: pd.DataFrame, comp: pd.Series[int], links: set[str] | None = None
) -> pd.DataFrame:
    """Per ring: sizes, amount, shared devices, mean GNN risk, labelled fraud rate.

    ``links``: the device values that link users; only those are listed as shared devices
    (a hub such as ``Windows`` may be shared inside a ring but did not link it)."""
    t = tx[tx[USER_ID].isin(comp.index)].assign(ring_id=lambda d: d[USER_ID].map(comp))
    with_device = t.dropna(subset=[DEVICE])
    if links is not None:
        with_device = with_device[with_device[DEVICE].astype(str).isin(links)]
    shared = (
        with_device.groupby(["ring_id", DEVICE], observed=True)[USER_ID]
        .nunique()
        .loc[lambda s: s >= 2]
        .reset_index()
        .groupby("ring_id")[DEVICE]
        .apply(lambda s: sorted(map(str, s)))
    )
    g = t.groupby("ring_id")
    rings = pd.DataFrame(
        {
            "uid_count": g[USER_ID].nunique(),
            "card_count": g[CARD_ID].nunique(),
            "tx_count": g.size(),
            "total_amount": g["TransactionAmt"].sum(),
            "mean_risk": g[FEATURE].mean(),
            "scored_tx": g[FEATURE].count(),
            "fraud_rate": g[LABEL].mean(),
            "first_dt": g["TransactionDT"].min(),
            "last_dt": g["TransactionDT"].max(),
            "uids": g[USER_ID].apply(lambda s: sorted(set(map(str, s)))),
        }
    )
    rings["shared_devices"] = shared.reindex(rings.index)
    return rings[rings["uid_count"] >= 2]


def split_ranked(rings: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """``(ranked, unscored)``: rings with >= 1 scored transaction by mean risk (then size);
    rings without any score by size, kept apart so they never sort silently to the bottom."""
    scored = rings["scored_tx"] > 0
    ranked = rings[scored].sort_values(["mean_risk", "tx_count"], ascending=False)
    unscored = rings[~scored].sort_values(["tx_count", "uid_count"], ascending=False)
    return ranked, unscored


def size_stats(rings: pd.DataFrame) -> dict[str, Any]:
    """Ring size distribution; a large ``largest_share`` means hub devices still link users."""
    sizes = rings["uid_count"]
    if sizes.empty:
        return {
            "rings": 0,
            "ring_uids": 0,
            "largest": 0,
            "largest_tx": 0,
            "largest_share": 0.0,
            "median": 0.0,
        }
    big = rings.loc[sizes.idxmax()]
    return {
        "rings": len(rings),
        "ring_uids": int(sizes.sum()),
        "largest": int(big["uid_count"]),
        "largest_tx": int(big["tx_count"]),
        "largest_share": float(big["uid_count"] / sizes.sum()),
        "median": float(sizes.median()),
    }


def render(ranked: pd.DataFrame, unscored: pd.DataFrame, stats: dict[str, Any]) -> str:
    top = ranked.head(TOP)
    lines = [
        "# Candidate fraud rings (GNN, batch)",
        "",
        "Users (`uid` = card + addr1) linked through shared `DeviceInfo` values; `card_id` is "
        "not used as a link (it is a coarse proxy). A device value links users only if at most "
        f"{stats['limit']} distinct uids share it, and a uid takes part only with at most "
        f"{stats['uid_device_limit']} distinct devices and {stats['uid_tx_limit']:,} transactions "
        f"({stats['hub_uids']:,} hub uids excluded). Ranked by mean GNN score (forward-chained "
        "scores on training rows, out-of-sample scores on validation rows); the labelled fraud "
        "rate is for evaluation only. Generated by `python -m src.graph.rings`.",
        "",
        f"Rings with >= 2 users: {stats['rings']:,}, holding {stats['ring_uids']:,} users; "
        f"median ring {stats['median']:.0f} users; largest ring {stats['largest']:,} users / "
        f"{stats['largest_tx']:,} transactions ({stats['largest_share']:.1%} of ring users). "
        f"Ranked (>= 1 scored transaction): {len(ranked):,}; **unscored: {len(unscored):,}** "
        "(all transactions in the first training block, which has no GNN score by design). "
        f"Labelled fraud rate overall {stats['base_rate']:.2%}; in the top {TOP} ranked rings "
        f"{stats['top_rate']:.2%}; in all rings {stats['all_rate']:.2%}.",
        "",
        "| Rank | Users | Cards | Transactions (scored) | Total amount | Shared devices "
        "| Mean GNN risk | Labelled fraud rate |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for rank, (_, r) in enumerate(top.iterrows(), start=1):
        dev = ", ".join(f"`{d}`" for d in (r["shared_devices"] or [])[:4])
        lines.append(
            f"| {rank} | {r['uid_count']} | {r['card_count']} | {r['tx_count']} "
            f"({r['scored_tx']}) | {r['total_amount']:,.2f} | {dev} | {r['mean_risk']:.4f} | "
            f"{r['fraud_rate']:.2%} |"
        )
    lines += [
        "",
        f"## Unscored rings ({len(unscored):,}; largest 10 by transactions, not ranked)",
        "",
        "| Users | Cards | Transactions | Total amount | Shared devices | Mean GNN risk "
        "| Labelled fraud rate |",
        "|---|---|---|---|---|---|---|",
    ]
    for _, r in unscored.head(10).iterrows():
        dev = ", ".join(f"`{d}`" for d in (r["shared_devices"] or [])[:4])
        lines.append(
            f"| {r['uid_count']} | {r['card_count']} | {r['tx_count']} | "
            f"{r['total_amount']:,.2f} | {dev} | unscored | {r['fraud_rate']:.2%} |"
        )
    lines += [
        "",
        "Caveat: `DeviceInfo` is a device model / OS string in this anonymized dataset, not a "
        "device identifier, so sharing it is weak evidence of a real ring.",
        "",
    ]
    return "\n".join(lines)


def write_table(dsn: str, rings: pd.DataFrame) -> int:
    """Replace the table with the top ranked (scored) rings."""
    import psycopg

    rows = [
        (
            rank,
            int(cast(int, ring_id)),
            int(r["uid_count"]),
            int(r["card_count"]),
            int(r["tx_count"]),
            float(r["total_amount"]),
            json.dumps(r["shared_devices"] or []),
            float(r["mean_risk"]),
            int(r["scored_tx"]),
            float(r["fraud_rate"]),
            int(r["first_dt"]),
            int(r["last_dt"]),
            json.dumps(r["uids"]),
        )
        for rank, (ring_id, r) in enumerate(rings.head(TOP).iterrows(), start=1)
    ]
    with psycopg.connect(dsn) as conn, conn.transaction():
        conn.execute(TABLE_DDL)
        conn.execute("DELETE FROM fraud_rings")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO fraud_rings (rank, ring_id, uid_count, card_count, tx_count, "
                "total_amount, shared_devices, mean_risk, scored_tx, fraud_rate, first_dt, "
                "last_dt, uids) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                rows,
            )
    return len(rows)


def main(argv: list[str] | None = None) -> pd.DataFrame:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-uid-limit", type=int, default=DEVICE_UID_LIMIT)
    parser.add_argument("--uid-device-limit", type=int, default=UID_DEVICE_LIMIT)
    parser.add_argument("--uid-tx-limit", type=int, default=UID_TX_LIMIT)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    df = load_ieee_cis(DEFAULT_RAW_DIR, usecols=[])
    keys = build_entity_keys(df)
    scores = pd.read_parquet(SCORES).set_index(KEY)[FEATURE]
    tx = df[[KEY, "TransactionDT", "TransactionAmt", DEVICE, LABEL]].assign(
        **{USER_ID: keys[USER_ID], CARD_ID: keys[CARD_ID], FEATURE: df[KEY].map(scores)}
    )
    hubs = hub_uids(tx.dropna(subset=[USER_ID]), args.uid_device_limit, args.uid_tx_limit)
    links = tx.dropna(subset=[USER_ID, DEVICE])
    usable = links[~links[USER_ID].isin(hubs)]
    comp = ring_components(usable, args.device_uid_limit)
    rings = summarize_rings(tx, comp, linking_devices(usable, args.device_uid_limit))
    ranked, unscored = split_ranked(rings)
    top = ranked.head(TOP)
    stats = size_stats(rings) | {
        "limit": args.device_uid_limit,
        "uid_device_limit": args.uid_device_limit,
        "uid_tx_limit": args.uid_tx_limit,
        "hub_uids": len(hubs),
        "base_rate": float(df[LABEL].mean()),
        "top_rate": float(
            (top["fraud_rate"] * top["tx_count"]).sum() / max(top["tx_count"].sum(), 1)
        ),
        "all_rate": float(
            (rings["fraud_rate"] * rings["tx_count"]).sum() / max(rings["tx_count"].sum(), 1)
        ),
    }
    REPORT.write_text(render(ranked, unscored, stats), encoding="utf-8")
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        logger.info("wrote %d rows to table fraud_rings", write_table(dsn, ranked))
    logger.info(
        "wrote %s rings=%d ranked=%d unscored=%d largest=%d users (%.1f%% of ring users)",
        REPORT,
        len(rings),
        len(ranked),
        len(unscored),
        stats["largest"],
        100 * stats["largest_share"],
    )
    return rings


if __name__ == "__main__":
    main()
