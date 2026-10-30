"""Load test of FastAPI ``POST /score`` with real validation transactions, on the bench copy.

Usage (after ``python -m scripts.bench_throughput setup --replay N`` and a temporary API that
scores into ``fraud_bench``; docs/evidence/2026-10-30-load-test/README.md)::

    python -m scripts.load_test_score --url http://127.0.0.1:18001 --run 1 [--levels 1,5,10,20]

* **Events:** the bench replay events recorded at setup (``reports/bench/setup.json``): real
  validation transactions, in (TransactionDT, TransactionID) order, built by the producer's
  own code (``stream_events``). Before the first request the script checks that none of them
  is in ``fraud_bench`` (expects 0), so every request is a fresh score.
* **Levels:** for each concurrency level, ``c`` workers send back-to-back requests for a
  ``--warmup`` (10 s) plus ``--duration`` (60 s) window; only requests *started* in the measured
  window count. Events are consumed in order from one cursor and never reused within a run;
  if they run out, the level stops early and says so.
* **Fresh scores only:** a response with ``duplicate = true`` (the read-only shortcut for an
  already stored transaction) stops the run. Pointed at the demo API by mistake, every event
  would be a duplicate there (all are stored in the demo DB), so the first response stops it
  and nothing is written.
* **Output:** per level requests, throughput, p50/p95/p99/max latency, error rate and duplicate
  count (JSON + printed table), and every request's latency (CSV).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import psycopg

from scripts.bench_throughput import BENCH_DB, STATE, VALIDATION_EVENTS, dsn
from src.data.load import DEFAULT_RAW_DIR
from src.streaming.producer import stream_events

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "reports" / "load_test"


@dataclass
class Sample:
    level: int
    phase: str  # warmup | measure
    transaction_id: int
    start_s: float  # since the level started
    latency_ms: float
    status: int  # HTTP status, 0 = transport error
    duplicate: bool
    error: str = ""


@dataclass
class Cursor:
    events: list[dict[str, Any]]
    next: int = 0
    exhausted_at: float | None = None

    def start_level(self) -> None:
        self.exhausted_at = None

    def take(self) -> dict[str, Any] | None:
        if self.next >= len(self.events):
            return None
        event = self.events[self.next]
        self.next += 1
        return event


@dataclass
class LevelResult:
    level: int
    samples: list[Sample] = field(default_factory=list)
    events_used: int = 0
    ran_out_after_s: float | None = None


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..100); None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def summarise(result: LevelResult, duration: float) -> dict[str, Any]:
    measured = [s for s in result.samples if s.phase == "measure"]
    ok = [s.latency_ms for s in measured if s.status == 200 and not s.error]
    errors = len(measured) - len(ok)
    window = duration if result.ran_out_after_s is None else max(result.ran_out_after_s, 1e-9)
    return {
        "concurrency": result.level,
        "requests": len(measured),
        "ok": len(ok),
        "errors": errors,
        "error_rate": errors / len(measured) if measured else None,
        "duplicates": sum(s.duplicate for s in result.samples),
        "throughput_rps": len(ok) / window if ok else 0.0,
        "p50_ms": percentile(ok, 50),
        "p95_ms": percentile(ok, 95),
        "p99_ms": percentile(ok, 99),
        "max_ms": max(ok) if ok else None,
        "events_used": result.events_used,
        "ran_out_of_events_after_s": result.ran_out_after_s,
        "warmup_requests": sum(s.phase == "warmup" for s in result.samples),
    }


class DuplicateError(RuntimeError):
    pass


async def run_level(
    client: httpx.AsyncClient, url: str, level: int, cursor: Cursor, warmup: float, duration: float
) -> LevelResult:
    result = LevelResult(level)
    start = time.perf_counter()
    end = start + warmup + duration
    first_event = cursor.next

    async def worker() -> None:
        while (t0 := time.perf_counter()) < end:
            event = cursor.take()
            if event is None:
                if cursor.exhausted_at is None:
                    cursor.exhausted_at = t0 - start
                return
            phase = "warmup" if t0 - start < warmup else "measure"
            status, duplicate, error = 0, False, ""
            try:
                response = await client.post(f"{url}/score", json=event)
                status = response.status_code
                if status == 200:
                    duplicate = bool(response.json().get("duplicate"))
                else:
                    error = response.text[:200]
            except httpx.HTTPError as exc:
                error = f"{type(exc).__name__}: {exc}"[:200]
            latency = (time.perf_counter() - t0) * 1000
            tx = int(event["TransactionID"])
            result.samples.append(
                Sample(level, phase, tx, t0 - start, latency, status, duplicate, error)
            )
            if duplicate:
                raise DuplicateError(
                    f"transaction {tx} came back duplicate=true: not a fresh score"
                )

    cursor.start_level()
    await asyncio.gather(*(worker() for _ in range(level)))
    result.events_used = cursor.next - first_event
    if cursor.exhausted_at is not None:
        result.ran_out_after_s = max(cursor.exhausted_at - warmup, 0.0)
    return result


def load_events() -> list[dict[str, Any]]:
    """The bench replay events, checked against setup.json and absent from fraud_bench."""
    state = json.loads(STATE.read_text(encoding="utf-8"))
    replay_ids: list[int] = state["replay_ids"]
    _, events = stream_events(
        str(DEFAULT_RAW_DIR), VALIDATION_EVENTS - state["replay"], state["replay"]
    )
    if [int(e["TransactionID"]) for e in events] != replay_ids:
        raise SystemExit("raw events do not match the replay ids recorded at setup")
    assert BENCH_DB == "fraud_bench"  # noqa: S101 - only ever counts in the bench copy
    with psycopg.connect(dsn(BENCH_DB), options="-c default_transaction_read_only=on") as conn:
        name = conn.execute("SELECT current_database()").fetchone()
        present = conn.execute(
            "SELECT count(*) FROM scored_transactions WHERE transaction_id = ANY(%s)", (replay_ids,)
        ).fetchone()
        conn.rollback()
    print(
        f"database {name[0] if name else '?'}: {len(replay_ids)} load-test events, "
        f"{present[0] if present else '?'} already present (expect 0)"
    )
    if not present or present[0] != 0:
        raise SystemExit("some load-test events are already scored in fraud_bench: reset first")
    return events


async def main_async(args: argparse.Namespace) -> list[dict[str, Any]]:
    events = load_events()
    cursor = Cursor(events)
    levels = [int(x) for x in args.levels.split(",")]
    summaries: list[dict[str, Any]] = []
    all_samples: list[Sample] = []
    limits = httpx.Limits(max_connections=max(levels), max_keepalive_connections=max(levels))
    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        health = (await client.get(f"{args.url}/health")).json()
        print(f"API {args.url} health: {health}")
        for level in levels:
            if cursor.next >= len(events):
                print(f"level {level}: no fresh events left; skipped")
                break
            result = await run_level(client, args.url, level, cursor, args.warmup, args.duration)
            s = summarise(result, args.duration)
            summaries.append(s)
            all_samples += result.samples
            print(
                f"run {args.run} c={level:>2}: {s['requests']:>5} req, "
                f"{s['throughput_rps']:6.1f} req/s, "
                f"p50 {s['p50_ms']:.1f} p95 {s['p95_ms']:.1f} p99 {s['p99_ms']:.1f} "
                f"max {s['max_ms']:.1f} ms, errors {s['errors']}, duplicates {s['duplicates']}, "
                f"events used {s['events_used']}"
                + (
                    f", RAN OUT after {s['ran_out_of_events_after_s']:.1f} s"
                    if s["ran_out_of_events_after_s"] is not None
                    else ""
                )
            )
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with (out / f"latencies_run{args.run}.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "run",
                "concurrency",
                "phase",
                "transaction_id",
                "start_s",
                "latency_ms",
                "status",
                "duplicate",
                "error",
            ]
        )
        for sample in all_samples:
            writer.writerow(
                [
                    args.run,
                    sample.level,
                    sample.phase,
                    sample.transaction_id,
                    f"{sample.start_s:.4f}",
                    f"{sample.latency_ms:.3f}",
                    sample.status,
                    sample.duplicate,
                    sample.error,
                ]
            )
    (out / f"summary_run{args.run}.json").write_text(
        json.dumps(
            {
                "run": args.run,
                "url": args.url,
                "warmup_s": args.warmup,
                "duration_s": args.duration,
                "levels": summaries,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return summaries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default="http://127.0.0.1:18001", help="the temporary bench API")
    parser.add_argument("--run", type=int, required=True)
    parser.add_argument("--levels", default="1,5,10,20")
    parser.add_argument("--warmup", type=float, default=10.0)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    if args.url.rstrip("/").endswith(":18000"):
        raise SystemExit("18000 is the demo API (demo database): use the temporary bench API")
    try:
        asyncio.run(main_async(args))
    except DuplicateError as exc:
        print(f"STOPPED: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
