"""Verify an exported Kafka incident cohort without accessing its broker or database."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def audit(root: Path, threshold: float) -> dict[str, Any]:
    manifest = (root / "source-sha256.txt").read_text().splitlines()
    require(bool(manifest), "Empty source manifest")
    names: set[str] = set()
    for entry in manifest:
        expected, name = entry.split(maxsplit=1)
        require(Path(name).name == name and name not in names, "Invalid manifest filename")
        names.add(name)
        with (root / name).open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        require(actual == expected, f"Checksum mismatch for {name}")
    require(
        {"committed-cohort.jsonl", "offset-reconciliation.jsonl", "frozen-group.txt"} <= names,
        "Incomplete source manifest",
    )

    boundaries: dict[int, tuple[int, int]] = {}
    for line in (root / "frozen-group.txt").read_text().splitlines():
        fields = line.split()
        if not fields or fields[0] == "GROUP":
            continue
        _, topic, partition, next_offset, end_offset, *_ = fields
        part, start, end = int(partition), int(next_offset), int(end_offset)
        require(
            topic == "transactions" and part not in boundaries and 0 <= start <= end,
            "Invalid frozen group positions",
        )
        boundaries[part] = (start, end)
    require(bool(boundaries), "No frozen group positions")

    source: dict[tuple[int, int], int] = {}
    with (root / "committed-cohort.jsonl").open() as stream:
        for part, offset, _, payload in map(json.loads, stream):
            key = (part, offset)
            require(key not in source, "Duplicate archived Kafka offset")
            source[key] = json.loads(base64.b64decode(payload, validate=True))["TransactionID"]
    require(
        source.keys()
        == {(part, offset) for part, (start, _) in boundaries.items() for offset in range(start)},
        "Archived Kafka positions do not match the frozen commits",
    )

    oracle: dict[int, tuple[float, bool]] = {}
    with (root / "expected-scores.jsonl").open() as stream:
        for tid, score, alert in map(json.loads, stream):
            require(tid not in oracle and alert == (score >= threshold), "Invalid oracle row")
            oracle[tid] = score, alert
    require(oracle.keys() == set(source.values()), "Oracle transaction IDs differ from the cohort")

    seen: set[tuple[int, int]] = set()
    stats: dict[int, Counter[str]] = {part: Counter() for part in boundaries}
    with (root / "offset-reconciliation.jsonl").open() as stream:
        for part, offset, tid, before in map(json.loads, stream):
            key = (part, offset)
            require(key not in seen and source.get(key) == tid, "SQL export differs from Kafka")
            seen.add(key)
            counts = stats[part]
            if before is None:
                counts["missing"] += 1
                continue
            score, alert, alert_row, pending = before
            expected_score, expected_alert = oracle[tid]
            counts.update(
                scored=1,
                alerts=alert_row,
                pending=pending,
                score_differences=score != expected_score,
                alert_differences=alert != expected_alert,
                inconsistent_alert_rows=alert != alert_row,
            )
    require(seen == source.keys(), "Incomplete SQL reconciliation export")
    digest_rows = [
        (tid, score, alert, threshold, alert) for tid, (score, alert) in sorted(oracle.items())
    ]
    digest = hashlib.sha256(json.dumps(digest_rows, separators=(",", ":")).encode()).hexdigest()
    return {
        "checksums": len(names),
        "committed_records": len(source),
        "unique_transaction_ids": len(oracle),
        "duplicate_deliveries": len(source) - len(oracle),
        "oracle_alerts": sum(alert for _, alert in oracle.values()),
        "oracle_sql_sha256": digest,
        "unexported_records": sum(end - start for start, end in boundaries.values()),
        "partitions": [
            {
                "partition": part,
                "next_committed_offset": start,
                "log_end_offset": end,
                **stats[part],
            }
            for part, (start, end) in sorted(boundaries.items())
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--threshold", required=True, type=float)
    args = parser.parse_args()
    print(json.dumps(audit(args.archive, args.threshold), indent=2))


if __name__ == "__main__":
    main()
