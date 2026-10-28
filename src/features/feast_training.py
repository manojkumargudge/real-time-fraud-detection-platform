"""Training model frame from the Feast offline store (``get_historical_features``).

Same rows, columns, order and dtypes as ``compare.build_model_frame(df, "baseline")``: raw
inputs come from the transactions themselves; the 12 history features come from a
point-in-time join of every transaction (its ``TransactionDT`` as event timestamp, its
``uid`` / ``card_id`` as entity keys) against per-event state snapshots in the offline store,
computed by the ``history_features`` on-demand view.

The offline store is written to ``repo_dir`` (default: a temporary directory); its online store
is a local SQLite file that is never materialized, so Redis and the demo stack are not touched.
"""

from __future__ import annotations

import bisect
import logging
import tempfile
import time
from pathlib import Path

import pandas as pd

from src.data.load import KEY
from src.features import feast_store as fs
from src.features.entity import CARD_ID, USER_ID
from src.features.history import TIME, HistoryConfig

logger = logging.getLogger(__name__)

FEATURE_SOURCES = ("pandas", "feast")
CHUNK_ROWS = 4_000


def entity_frame(events: list[fs.EntityEvent], config: HistoryConfig) -> pd.DataFrame:
    """One entity row per transaction, at the transaction's own timestamp."""
    return pd.DataFrame(
        [
            fs.request_row(e.event, e.card, e.uid, config.change_columns)
            | {
                fs.EVENT_TIMESTAMP: fs.to_datetime(e.event[TIME]),
                KEY: int(e.event[KEY]),
            }
            for e in events
        ]
    )


def feast_history_features(
    df: pd.DataFrame, config: HistoryConfig, repo_dir: Path, *, chunk_rows: int = CHUNK_ROWS
) -> pd.DataFrame:
    """History features for every row of ``df`` from Feast, aligned to ``df.index`` with the
    dtypes of ``compute_history_features``.

    ``get_historical_features`` reads the two state views (point-in-time join on ``uid`` /
    ``card_id`` and the transaction's timestamp); the features are computed from the joined
    state by ``odfv_features``, the function the ``history_features`` on-demand view runs, as
    serving does. Feast's python-mode on-demand view and its file offline store do not scale to
    the full file (dask re-tokenizes the frames on every step; one call against all 590,540
    snapshots costs minutes), so entity rows are retrieved in time-ordered chunks. Each chunk's
    offline source holds, for the chunk's entities only, the latest snapshot before the chunk
    and every snapshot up to its last timestamp: the rows a point-in-time join can pick, so the
    result equals a single join over all snapshots.
    """
    started = time.perf_counter()
    events = sorted(
        fs.events_from_frame(df, config), key=lambda e: (int(e.event[TIME]), int(e.event[KEY]))
    )
    uid_rows, card_rows = fs.fold_states(events, config, snapshots=True)
    keyed = [e for e in events if e.uid is not None and e.card is not None]
    snap_ts = [int(e.event[TIME]) for e in keyed]
    store = fs.open_store(repo_dir, config=config)
    state_refs = [r for r in fs.feature_refs(config) if not r.startswith(f"{fs.ODFV}:")]
    request = entity_frame(events, config)
    join_cols = [USER_ID, CARD_ID, fs.EVENT_TIMESTAMP, KEY]

    last_uid: dict[str, int] = {}
    last_card: dict[str, int] = {}
    passed = 0  # snapshots strictly before the current chunk, folded into last_*
    parts = []
    for lo in range(0, len(events), chunk_rows):
        chunk = events[lo : lo + chunk_rows]
        t_min, t_max = int(chunk[0].event[TIME]), int(chunk[-1].event[TIME])
        while passed < len(keyed) and snap_ts[passed] < t_min:
            last_uid[keyed[passed].uid] = passed  # type: ignore[index]
            last_card[keyed[passed].card] = passed  # type: ignore[index]
            passed += 1
        upto = bisect.bisect_right(snap_ts, t_max)
        uids = {e.uid for e in chunk if e.uid is not None}
        cards = {e.card for e in chunk if e.card is not None}
        sel_uid = [uid_rows[last_uid[u]] for u in uids if u in last_uid]
        sel_card = [card_rows[last_card[c]] for c in cards if c in last_card]
        for j in range(passed, upto):
            if keyed[j].uid in uids:
                sel_uid.append(uid_rows[j])
            if keyed[j].card in cards:
                sel_card.append(card_rows[j])
        fs.write_offline(repo_dir, sel_uid, sel_card, config=config)
        part = request.iloc[lo : lo + chunk_rows]
        got = fs.historical_features(store, part[join_cols], config, state_refs)
        parts.append(got.drop(columns=[c for c in join_cols if c in got.columns]))
        logger.info("feast chunk rows=%d..%d of %d", lo, lo + len(chunk), len(events))

    state = pd.concat(parts)
    joined = request.set_index(KEY).join(state, how="left")
    inputs = joined.astype(object).where(joined.notna(), None)  # missing state -> None
    names = config.feature_names()
    feats = pd.DataFrame(
        fs.odfv_features({c: inputs[c].tolist() for c in inputs.columns}, config),
        index=joined.index,
    )
    feats = feats.loc[df[KEY].to_numpy(), names].astype("float64").astype("float32")
    feats.index = df.index
    logger.info(
        "feast historical features rows=%d seconds=%.1f",
        len(feats),
        time.perf_counter() - started,
    )
    return feats


def feast_model_frame(
    df: pd.DataFrame,
    config: HistoryConfig,
    repo_dir: Path | None = None,
    *,
    chunk_rows: int = CHUNK_ROWS,
) -> pd.DataFrame:
    """``build_model_frame(df, "baseline", config)`` with the history features read from Feast."""
    from src.models.compare import raw_model_columns

    if repo_dir is not None:
        hist = feast_history_features(df, config, repo_dir, chunk_rows=chunk_rows)
    else:
        with tempfile.TemporaryDirectory(prefix="feast-train-", ignore_cleanup_errors=True) as tmp:
            hist = feast_history_features(df, config, Path(tmp), chunk_rows=chunk_rows)
    return pd.concat([raw_model_columns(df), hist[config.feature_names()]], axis=1)


def frame_sha256(frame: pd.DataFrame) -> str:
    """Digest of a frame's column names, dtypes, index and values (NaN-aware, order-sensitive)."""
    import hashlib

    h = hashlib.sha256()
    h.update(repr([(str(c), str(t)) for c, t in frame.dtypes.items()]).encode())
    h.update(pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes())
    return h.hexdigest()


def compare_frames(a: pd.DataFrame, b: pd.DataFrame) -> dict[str, object]:
    """Exact comparison: rows, column names/order, dtypes, index and values (NaN == NaN).
    ``max_abs_diff`` lists numeric columns whose values differ."""
    import numpy as np

    if a.empty or b.empty:
        raise ValueError(f"cannot compare an empty frame (rows {len(a)} vs {len(b)})")
    same_shape = a.shape == b.shape and list(a.columns) == list(b.columns)
    diffs: dict[str, float] = {}
    differing: list[str] = []
    if same_shape and a.index.equals(b.index):
        for col in a.columns:
            x, y = a[col], b[col]
            if x.equals(y):
                continue
            differing.append(str(col))
            if pd.api.types.is_numeric_dtype(x) and pd.api.types.is_numeric_dtype(y):
                d = np.abs(x.to_numpy(np.float64) - y.to_numpy(np.float64))
                both_nan = x.isna().to_numpy() & y.isna().to_numpy()
                d[both_nan] = 0.0
                diffs[str(col)] = float(np.nanmax(d)) if not np.isnan(d).all() else float("nan")
    return {
        "rows": (len(a), len(b)),
        "same_columns": list(a.columns) == list(b.columns),
        "same_dtypes": same_shape and bool((a.dtypes == b.dtypes).all()),
        "same_index": a.index.equals(b.index),
        "differing_columns": differing,
        "max_abs_diff": diffs,
        "identical": same_shape and a.equals(b) and bool((a.dtypes == b.dtypes).all()),
        "sha256": (frame_sha256(a), frame_sha256(b)),
    }
