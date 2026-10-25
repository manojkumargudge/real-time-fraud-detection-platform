"""Export validation-period events for the Kubernetes smoke test (docs/kubernetes.md).

Usage: ``python -m scripts.k8s_events [--skip 0] [--limit 20] [--out k8s/local/events.json]``

The events are exactly what ``python -m src.streaming.producer --skip S --limit N`` would send
(validation period, time order, labels and D1 removed). The output contains transaction data, so
it goes to the gitignored ``k8s/local/`` and into the cluster only as a ConfigMap created at
deploy time.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.data.load import DEFAULT_RAW_DIR
from src.streaming.producer import stream_events

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    parser.add_argument("--skip", type=int, default=0, help="validation events to skip")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--out", type=Path, default=ROOT / "k8s" / "local" / "events.json")
    args = parser.parse_args(argv)
    _, events = stream_events(args.raw_dir, args.skip, args.limit)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(events, allow_nan=False), encoding="utf-8")
    ids = [e["TransactionID"] for e in events]
    print(f"wrote {len(events)} events to {args.out} (TransactionID {ids[0]}..{ids[-1]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
