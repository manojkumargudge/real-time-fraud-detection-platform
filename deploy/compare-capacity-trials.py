"""Evaluate the same-host sustained HTTP gates after all four trials finish."""

import argparse
import json
from pathlib import Path
from statistics import median

LEVELS = ("spread-1", "spread-4", "spread-8", "hot-8")


def read_level(trial: Path, level: str) -> dict:
    if not (trial / "complete").is_file():
        raise ValueError(f"Incomplete trial: {trial}")
    (row,) = json.loads((trial / f"{level}.json").read_text())
    if row["measured_end_unix"] - row["measured_start_unix"] < 600:
        raise ValueError(f"Measurement shorter than 600 seconds: {trial}/{level}")
    if any(row[key] for key in ("errors", "timeouts", "duplicates")):
        raise ValueError(f"Errors, timeouts or duplicates in {trial}/{level}")
    if row["throughput_rps"] <= 0 or row["p95_ms"] <= 0:
        raise ValueError(f"Invalid throughput or latency in {trial}/{level}")
    return row


def compare(session: Path) -> dict:
    metrics: dict = {}
    for group in ("main", "release"):
        metrics[group] = {}
        for level in LEVELS:
            rows = [read_level(session / f"{group}-{i}", level) for i in (1, 2)]
            metrics[group][level] = {
                key: median(row[key] for row in rows)
                for key in ("throughput_rps", "p50_ms", "p95_ms", "p99_ms")
            }
    main = metrics["main"]
    release = metrics["release"]
    gates = {
        f"{level}_throughput": release[level]["throughput_rps"]
        >= 0.95 * main[level]["throughput_rps"]
        for level in ("spread-1", "spread-8")
    }
    gates["spread-1_p95"] = release["spread-1"]["p95_ms"] <= 1.10 * main["spread-1"]["p95_ms"]
    return {"metrics": metrics, "gates": gates, "passed": all(gates.values())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    args = parser.parse_args()
    result = compare(args.session)
    print(json.dumps(result, indent=2))
    if not result["passed"]:
        raise SystemExit("Sustained relative throughput or latency gate failed")


if __name__ == "__main__":
    main()
