"""Transaction-entity graph for the GNN (batch only; never used by serving).

Nodes: one ``tx`` node per transaction, plus one node per distinct value of each entity column
(``ENTITY_COLUMNS``). Edges: ``tx -> entity`` for every non-missing, non-hub value, and the
reverse edges (added in ``src.graph.data``).

``account=True`` (``--graph account``) adds an ``account`` node per ``uid`` (card + addr1) with
``tx -> account`` edges (same hub rule) and ``account -> card`` edges (non-hub cards only). A uid
with a missing ``addr1`` (the ``<NA>`` token) gets no account node: it is a card's bucket of
address-less transactions, not an account. There is no direct account -> device edge: entity
nodes have time 0 under node-level temporal sampling, so a static account -> device edge would
expose devices the account uses later; accounts reach devices through their transactions.
The default graph (``account=False``) is unchanged.

Leakage rules (tests in tests/test_graph.py):

* Hubs (entity values linked to more than ``degree_limit`` transactions, e.g. ``gmail.com``)
  are dropped. The degree is counted on **training rows only** (``TransactionDT`` < cut-off), so
  validation data never influences which values are kept.
* ``include_validation=False`` builds the training graph from rows before the cut-off only;
  ``True`` adds validation rows as nodes/edges -- their labels are never put in the graph
  (``labels`` is -1 for them) and are returned separately for evaluation only.
* Node features are the XGBoost model inputs (``compare.feature_columns``): no label, no D1, no
  raw time, no IDs; the categorical encoder and numeric scaling are fit on the fit split
  (training rows before the training holdout) only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.array_types import FloatArray, IntArray
from src.data.load import KEY, LABEL
from src.features.entity import CARD_ID, USER_ID, build_entity_keys
from src.features.history import TIME, HistoryConfig
from src.features.preprocess import DEFAULT_CATEGORICAL, CategoricalEncoder
from src.features.split import time_cutoff, validation_start
from src.models.compare import RAW_NUMERIC, build_model_frame

logger = logging.getLogger(__name__)

# node type -> source column
ENTITY_COLUMNS: dict[str, str] = {
    "card": CARD_ID,
    "device": "DeviceInfo",
    "p_email": "P_emaildomain",
    "r_email": "R_emaildomain",
    "addr1": "addr1",
}
ACCOUNT = "account"  # uid node type, only in the account graph
ACCOUNT_CARD = (ACCOUNT, "uses_card", "card")  # entity -> entity edge type
GRAPHS = ("default", "account")  # --graph choices
DEFAULT_DEGREE_LIMIT = 1_000  # training transactions per entity value
HOLDOUT_FRACTION = 0.2  # last 20% of training time: early stopping / threshold (like XGBoost)
NO_LABEL = -1
GNN_SCORE = "gnn_score"  # column of the per-transaction GNN score table (src.graph.stack)
GNN_SCORES_PATH = Path(__file__).resolve().parents[2] / "artifacts" / "gnn" / "scores.parquet"
GNN_ACCOUNT_SCORES_PATH = GNN_SCORES_PATH.parents[1] / "gnn-account" / "scores.parquet"


@dataclass(frozen=True)
class GraphSplit:
    cutoff: int  # validation start: TransactionDT >= cutoff is validation
    holdout_start: int  # training holdout: holdout_start <= TransactionDT < cutoff


@dataclass
class TxGraph:
    """Arrays for the heterogeneous graph (converted to torch in ``src.graph.data``)."""

    tx_id: IntArray  # TransactionID per tx node
    tx_time: IntArray  # TransactionDT per tx node
    x_num: FloatArray  # scaled numeric features (+ missing indicators)
    x_cat: IntArray  # categorical embedding indices (0 = missing, 1 = unseen, 2+ = value)
    labels: IntArray  # isFraud for training rows, NO_LABEL for validation rows
    is_fit: np.ndarray[Any, np.dtype[np.bool_]]
    is_holdout: np.ndarray[Any, np.dtype[np.bool_]]
    is_valid: np.ndarray[Any, np.dtype[np.bool_]]
    entity_names: dict[str, list[str]]  # node type -> value per entity node
    edges: dict[str, IntArray]  # node type -> (2, E) [tx index, entity index]
    split: GraphSplit
    numeric_columns: list[str]
    categorical_columns: list[str]
    category_sizes: list[int]
    hubs: dict[str, list[tuple[str, int]]] = field(default_factory=dict)  # dropped values
    # (src, rel, dst) -> (2, E) [src entity index, dst entity index]; account graph only
    entity_edges: dict[tuple[str, str, str], IntArray] = field(default_factory=dict)

    @property
    def num_tx(self) -> int:
        return len(self.tx_id)


def split_times(df: pd.DataFrame) -> GraphSplit:
    cutoff = validation_start(df[TIME])
    train_dt = df.loc[df[TIME] < cutoff, TIME]
    return GraphSplit(cutoff=cutoff, holdout_start=time_cutoff(train_dt, HOLDOUT_FRACTION))


def entity_frame(df: pd.DataFrame, *, account: bool = False) -> pd.DataFrame:
    """One string column per entity node type (None = no edge); ``account`` adds the uid,
    None where ``addr1`` is missing (``<NA>`` uids get no account node)."""
    out = pd.DataFrame(index=df.index)
    keys = build_entity_keys(df)
    for node_type, col in ENTITY_COLUMNS.items():
        s = keys[CARD_ID] if col == CARD_ID else df[col]
        if pd.api.types.is_numeric_dtype(s):
            s = s.map(lambda v: None if pd.isna(v) else repr(float(v)))
        out[node_type] = s.astype(object).where(s.notna(), None)
    if account:
        uid = keys[USER_ID]
        out[ACCOUNT] = uid.astype(object).where(uid.notna() & df["addr1"].notna(), None)
    return out


def hub_values(
    entities: pd.DataFrame, train_mask: pd.Series[bool], degree_limit: int
) -> dict[str, list[tuple[str, int]]]:
    """Values linked to more than ``degree_limit`` *training* transactions, per node type."""
    hubs: dict[str, list[tuple[str, int]]] = {}
    for node_type in entities.columns:
        counts = entities.loc[train_mask, node_type].dropna().value_counts()
        over = counts[counts > degree_limit]
        hubs[node_type] = [(str(k), int(v)) for k, v in over.items()]
    return hubs


def _scale_numeric(frame: pd.DataFrame, cols: list[str], fit: np.ndarray[Any, Any]) -> FloatArray:
    """Signed log1p, then standardise with fit-split statistics; NaN -> 0 plus an indicator."""
    raw = frame[cols].to_numpy(np.float64)
    v = np.sign(raw) * np.log1p(np.abs(raw))
    mean = np.nanmean(v[fit], axis=0)
    std = np.nanstd(v[fit], axis=0)
    std = np.where(np.isfinite(std) & (std > 0), std, 1.0)
    mean = np.where(np.isfinite(mean), mean, 0.0)
    z = (v - mean) / std
    missing = np.isnan(z)
    z[missing] = 0.0
    out: FloatArray = np.hstack([z, missing.astype(np.float64)]).astype(np.float32)
    return out


def build_graph(
    df: pd.DataFrame,
    *,
    include_validation: bool,
    degree_limit: int = DEFAULT_DEGREE_LIMIT,
    config: HistoryConfig | None = None,
    account: bool = False,
) -> tuple[TxGraph, IntArray | None]:
    """``(graph, validation_labels)``; the labels are None for the training graph and are
    returned *outside* the graph so nothing that consumes the graph can read them."""
    config = config or HistoryConfig()
    split = split_times(df)
    frame = build_model_frame(df, "baseline", config)
    entities = entity_frame(df, account=account)
    train_mask = df[TIME] < split.cutoff
    keep = train_mask | include_validation
    frame, entities, sub = frame[keep], entities[keep], df[keep]
    t = sub[TIME].to_numpy(np.int64)
    is_valid = t >= split.cutoff
    is_holdout = (t >= split.holdout_start) & ~is_valid
    is_fit = ~is_valid & ~is_holdout

    numeric = [*RAW_NUMERIC, *config.feature_names()]
    x_num = _scale_numeric(frame, numeric, is_fit)
    cat_cols = list(DEFAULT_CATEGORICAL)
    encoder = CategoricalEncoder().fit(frame[is_fit])
    codes = encoder.transform(frame[cat_cols]).to_numpy(np.float64)
    x_cat = np.where(np.isnan(codes), 0, codes + 2).astype(np.int64)  # -1 (unseen) -> 1
    vocab = encoder.vocab_ or {}
    sizes = [len(vocab.get(c, {})) + 2 for c in cat_cols]

    labels = np.where(is_valid, NO_LABEL, sub[LABEL].to_numpy(np.int64)).astype(np.int64)
    valid_labels = sub.loc[is_valid, LABEL].to_numpy(np.int64) if include_validation else None

    hubs = hub_values(entities, pd.Series(~is_valid, index=entities.index), degree_limit)
    names: dict[str, list[str]] = {}
    edges: dict[str, IntArray] = {}
    for node_type in entities.columns:
        values = entities[node_type]
        dropped = {v for v, _ in hubs[node_type]}
        usable = values.notna() & ~values.isin(dropped)
        codes_e, uniques = pd.factorize(values[usable], sort=True)
        names[node_type] = [str(u) for u in uniques]
        tx_index = np.flatnonzero(usable.to_numpy())
        edges[node_type] = np.vstack([tx_index, codes_e]).astype(np.int64)

    graph = TxGraph(
        tx_id=sub[KEY].to_numpy(np.int64),
        tx_time=t,
        x_num=x_num,
        x_cat=x_cat,
        labels=labels,
        is_fit=is_fit,
        is_holdout=is_holdout,
        is_valid=is_valid,
        entity_names=names,
        edges=edges,
        split=split,
        numeric_columns=numeric,
        categorical_columns=cat_cols,
        category_sizes=sizes,
        hubs=hubs,
        entity_edges={ACCOUNT_CARD: account_card_edges(entities, names)} if account else {},
    )
    logger.info(
        "graph tx=%d (valid=%d) entities=%s edges=%s entity_edges=%s hubs=%s",
        graph.num_tx,
        int(is_valid.sum()),
        {k: len(v) for k, v in names.items()},
        {k: v.shape[1] for k, v in edges.items()},
        {"__".join(k): v.shape[1] for k, v in graph.entity_edges.items()},
        {k: len(v) for k, v in hubs.items()},
    )
    return graph, valid_labels


def account_card_edges(entities: pd.DataFrame, names: dict[str, list[str]]) -> IntArray:
    """``(2, E)`` [account index, card index] for each account whose card is a (non-hub) node.

    A uid contains its card, so each account has exactly one card and the edge says nothing
    about the future (unlike an account -> device edge)."""
    pairs = entities[[ACCOUNT, "card"]].dropna().drop_duplicates()
    a = pairs[ACCOUNT].map({n: i for i, n in enumerate(names[ACCOUNT])}).fillna(-1).to_numpy()
    c = pairs["card"].map({n: i for i, n in enumerate(names["card"])}).fillna(-1).to_numpy()
    keep = (a >= 0) & (c >= 0)  # hub accounts / hub cards have no node
    order = np.lexsort((c[keep], a[keep]))
    return np.vstack([a[keep][order], c[keep][order]]).astype(np.int64)


def default_scores_path(graph: str) -> Path:
    return GNN_ACCOUNT_SCORES_PATH if graph == ACCOUNT else GNN_SCORES_PATH


def checked_scores_path(graph: str, path: Path | None) -> Path:
    """Score table for ``--graph``/``--scores``. A non-default graph never writes (or claims)
    ``GNN_SCORES_PATH``: that file is the input of the stored rings."""
    path = path or default_scores_path(graph)
    if graph != "default" and path.resolve() == GNN_SCORES_PATH.resolve():
        raise SystemExit(f"--graph {graph} must not use {GNN_SCORES_PATH}")
    return path


def report_path(base: Path, graph: str) -> Path:
    """``reports/x.md`` for the default graph, ``reports/x_<graph>.md`` otherwise."""
    return base if graph == "default" else base.with_name(f"{base.stem}_{graph}{base.suffix}")
