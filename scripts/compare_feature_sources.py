"""Prove the Feast offline training matrix equals the pandas one champion v2 was trained on.

Usage: ``python -m scripts.compare_feature_sources [--limit N]``. Rebuilds the v2 recipe
(``build_model_frame(df, "baseline")`` -> ``chronological_split(valid_fraction=0.2)``) from both
feature sources and compares the training rows exactly: row count, column names and order,
dtypes, values (max abs difference per differing column) and a sha256 of each. Also compares
the encoded float32 XGBoost matrix and scores both validation matrices with the served v2
booster (read-only; no training, no MLflow access). ``--limit`` takes the earliest N
transactions (a quick slice; the v2 checks then do not apply).
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import numpy as np
import xgboost as xgb

from src.data.load import DEFAULT_RAW_DIR, LABEL, load_ieee_cis
from src.features.feast_training import compare_frames, feast_model_frame, frame_sha256
from src.features.history import TIME, HistoryConfig
from src.features.preprocess import CategoricalEncoder
from src.features.split import chronological_split
from src.models.compare import build_model_frame, evaluate, feature_columns
from src.models.train import ARTIFACTS_DIR, METADATA_FILE, MODEL_FILE


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--bundle-dir", type=Path, default=ARTIFACTS_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = HistoryConfig()
    df = load_ieee_cis(DEFAULT_RAW_DIR, usecols=[])
    if args.limit:
        df = df.sort_values([TIME, "TransactionID"]).head(args.limit)
    print(f"transactions: {len(df)}")

    t0 = time.perf_counter()
    pandas_frame = build_model_frame(df, "baseline", config)
    t1 = time.perf_counter()
    feast_frame = feast_model_frame(df, config)
    t2 = time.perf_counter()
    print(f"pandas frame {t1 - t0:.1f}s, feast frame {t2 - t1:.1f}s")

    p_train, p_valid, p_b = chronological_split(pandas_frame, valid_fraction=0.2)
    f_train, f_valid, f_b = chronological_split(feast_frame, valid_fraction=0.2)
    assert p_b == f_b, (p_b, f_b)
    cols = feature_columns(config)
    print(f"split: {p_b}; train rows pandas={len(p_train)} feast={len(f_train)}")

    report: dict[str, object] = {"boundary": str(p_b)}
    for name, a, b in (
        ("train_frame", p_train, f_train),
        ("valid_frame", p_valid, f_valid),
    ):
        report[name] = compare_frames(a, b)
    enc_p, enc_f = CategoricalEncoder().fit(p_train), CategoricalEncoder().fit(f_train)
    assert enc_p.to_dict() == enc_f.to_dict()
    x_p = enc_p.transform(p_train[cols]).astype("float32")
    x_f = enc_f.transform(f_train[cols]).astype("float32")
    report["train_matrix_float32"] = compare_frames(x_p, x_f)
    print(json.dumps(report, indent=2, default=str))

    if args.limit is None:
        meta = json.loads((args.bundle_dir / METADATA_FILE).read_text(encoding="utf-8"))
        split = meta["split"]
        print(
            "v2 metadata: train_end",
            split["train_end"],
            "train_rows",
            split["train_rows"],
            "valid_rows",
            split["valid_rows"],
        )
        assert (p_b.train_end, len(p_train), len(p_valid)) == (
            split["train_end"],
            split["train_rows"],
            split["valid_rows"],
        )
        assert enc_p.to_dict() == meta["encoder"], "encoder vocabulary differs from v2's"
        print(f"train fraud rate {p_train[LABEL].mean():.6f} (v2 param train_fraud_rate 0.035135)")
        booster = xgb.Booster()
        booster.load_model(args.bundle_dir / MODEL_FILE)
        enc_v2 = CategoricalEncoder.from_dict(meta["encoder"])
        y = p_valid[LABEL].to_numpy(np.int64)
        for label, valid in (("pandas", p_valid), ("feast", f_valid)):
            x = enc_v2.transform(valid[cols]).astype("float32")
            s = booster.predict(xgb.DMatrix(x, missing=np.nan))
            m = evaluate(y, s, meta["threshold"])
            print(
                f"v2 on {label} validation: PR-AUC {m['average_precision']:.6f} "
                f"(metadata {meta['validation_metrics']['average_precision']:.6f}), "
                f"scores sha256 {frame_sha256(__import__('pandas').DataFrame({'s': s}))[:16]}"
            )
    identical = all(report[k]["identical"] for k in report if k != "boundary")  # type: ignore[index]
    print("IDENTICAL:", identical)


if __name__ == "__main__":
    main()
