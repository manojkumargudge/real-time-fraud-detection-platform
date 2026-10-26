"""Consumer throughput benchmark on a clone of the demo database (manual, not part of CI).

Usage (repo root, demo stack running)::

    python -m scripts.bench_throughput setup [--replay 4000]
    python -m scripts.bench_throughput run --label before-1 --index 0 --limit 1000 --rate 5
    python -m scripts.bench_throughput run --label before-2 --index 1000 --limit 1000
    python -m scripts.bench_throughput restart      # rebuild + restart the bench consumers
    python -m scripts.bench_throughput reset        # back to the post-setup state
    python -m scripts.bench_throughput teardown

Setup (docker-compose.bench.yml, docs/serving.md "Throughput benchmark"):
- ``fraud_bench`` = ``pg_dump`` of the demo database ``fraud`` restored on the same PostgreSQL;
  the demo database is only read. The last ``--replay`` events by (transaction time, id) are
  deleted from fraud_bench (alerts first): they are the replay events, the rest stays as history.
- Feast state is built into a separate Redis (``bench-redis``, project ``fraud-bench``) from the
  batch transactions strictly before the first replay event -- the same events fraud_bench holds
  (checked: identical transaction IDs).
- 3 consumers (image built from this checkout, ``FEATURE_SOURCE=feast``, ``STAGE_TIMINGS=1``) on
  the demo network, topic ``bench-transactions``, group ``bench-scorer``.

Safety: every DELETE / TRUNCATE / DROP first prints the current database name and aborts unless
it is fraud_bench. Kafka deletes are limited to the ``bench-`` topic and group.

``run`` replays replay events ``[index, index + limit)`` (``--rate`` events/s, default
unthrottled) and reports end-to-end latency (produce -> ``scored_at``; the producer runs in
a container, so both times come from the Docker VM clock), throughput, per-stage p50/p95
from the consumers' ``stages`` lines,
scorer latency from the ``scored`` lines and CPU per container (``docker stats``). The wall clock
is logged every 5 s; a gap over 30 s (laptop suspend) marks the run as discarded. Results go to
``reports/bench/<label>.json`` (gitignored).
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import statistics
import subprocess  # nosec B404 - fixed docker/python commands, no shell, no user input
import sys
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from confluent_kafka.admin import AdminClient, NewTopic

from src.data.load import DEFAULT_RAW_DIR
from src.streaming.events import SCHEMA_HEADER, encode
from src.streaming.producer import stream_events

ROOT = Path(__file__).resolve().parents[1]
DEMO = ["docker", "compose"]
BENCH = ["docker", "compose", "-f", "docker-compose.bench.yml"]
BENCH_DB = "fraud_bench"
TOPIC, DLQ, GROUP = "bench-transactions", "bench-transactions.dlq", "bench-scorer"
KAFKA = "localhost:9092"  # demo broker, host listener
FEAST_DIR = ROOT / "artifacts" / "feast-bench"
OUT = ROOT / "reports" / "bench"
STATE = OUT / "setup.json"
WATCHED = ("fraud-bench-", "fraud-platform-postgres-1", "fraud-platform-kafka-1")
VALIDATION_EVENTS = 118_108  # the whole validation window, as streamed into the demo database


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


def sh(cmd: list[str], **kw: Any) -> str:
    done = subprocess.run(cmd, cwd=ROOT, check=True, capture_output=True, text=True, **kw)  # nosec B603
    return str(done.stdout)


def env_file() -> dict[str, str]:
    values = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip().strip('"')
    return values


def dsn(database: str) -> str:
    env = env_file()  # the password is never printed
    port = env.get("POSTGRES_HOST_PORT", "15432")
    return f"postgresql://fraud:{env['POSTGRES_PASSWORD']}@localhost:{port}/{database}"


def guard(conn: psycopg.Connection[Any]) -> None:
    """Safety rule: print the current database and abort unless it is fraud_bench."""
    row = conn.execute("SELECT current_database()").fetchone()
    name = row[0] if row else None
    print(f"  current database: {name}", flush=True)
    if name != BENCH_DB:
        raise SystemExit(f"ABORT: destructive statement refused on database {name!r}")


def delete_replay_rows(conn: psycopg.Connection[Any], ids: list[int]) -> tuple[int, int, int]:
    """Remove the replay events (and their alerts) from fraud_bench: ``(reviews, alerts, rows)``.

    The demo database (the clone's source) may hold analyst reviews (``case_reviews``). They
    reference alerts, and the table is append-only (a trigger rejects row DELETE), so the clone's
    reviews are emptied with TRUNCATE (row triggers do not fire) before alerts are deleted. The
    benchmark never needs reviews. Each statement is guarded.
    """
    with conn.transaction():
        reviews = 0
        if conn.execute("SELECT to_regclass('case_reviews')").fetchone() not in (None, (None,)):
            guard(conn)
            reviews = int(conn.execute("SELECT count(*) FROM case_reviews").fetchone()[0])  # type: ignore[index]
            guard(conn)
            conn.execute("TRUNCATE case_reviews")
        guard(conn)
        alerts = conn.execute("DELETE FROM alerts WHERE transaction_id = ANY(%s)", (ids,)).rowcount
        guard(conn)
        rows = conn.execute(
            "DELETE FROM scored_transactions WHERE transaction_id = ANY(%s)", (ids,)
        ).rowcount
    return reviews, alerts, rows


def db_exists(name: str) -> bool:
    with psycopg.connect(dsn("postgres"), autocommit=True) as conn:
        return (
            conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
            is not None
        )


def drop_bench_db() -> None:
    """DROP DATABASE must run from another database: verify the target's identity with the guard
    (connected to it), then drop exactly that name from the maintenance database."""
    if not db_exists(BENCH_DB):
        return
    with psycopg.connect(dsn(BENCH_DB), autocommit=True) as conn:
        guard(conn)
    with psycopg.connect(dsn("postgres"), autocommit=True) as conn:
        assert BENCH_DB == "fraud_bench"  # noqa: S101 - hard stop if the constant ever changes
        log(f"DROP DATABASE {BENCH_DB}")
        conn.execute("DROP DATABASE fraud_bench WITH (FORCE)")


# --------------------------------------------------------------------------- setup


def setup(replay: int) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if db_exists(BENCH_DB):
        raise SystemExit(f"{BENCH_DB} already exists; run teardown first")
    log("cloning demo database fraud -> fraud_bench (pg_dump | pg_restore, demo only read)")
    sh(
        [
            *DEMO,
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            "createdb -U fraud fraud_bench && "
            "pg_dump -U fraud -Fc fraud | pg_restore -U fraud -d fraud_bench --no-owner",
        ]
    )
    log("loading the validation events from the raw data")
    _, events = stream_events(str(DEFAULT_RAW_DIR), 0, VALIDATION_EVENTS)
    keep, replayed = events[:-replay], events[-replay:]
    cut_dt = int(replayed[0]["TransactionDT"])
    if int(keep[-1]["TransactionDT"]) == cut_dt:
        raise SystemExit(f"replay boundary splits TransactionDT {cut_dt}; choose another --replay")
    replay_ids = [int(e["TransactionID"]) for e in replayed]
    with psycopg.connect(dsn(BENCH_DB)) as conn:
        demo_like = conn.execute(
            "SELECT transaction_id FROM scored_transactions "
            "ORDER BY transaction_dt DESC, transaction_id DESC LIMIT %s",
            (replay,),
        ).fetchall()
        if sorted(r[0] for r in demo_like) != sorted(replay_ids):
            raise SystemExit(
                "the last events in fraud_bench differ from the raw data's last events"
            )
        log(f"deleting the last {replay} events (TransactionDT >= {cut_dt}) from fraud_bench")
        reviews, alerts, rows = delete_replay_rows(conn, replay_ids)
        if rows != replay:
            raise SystemExit(f"deleted {rows} rows, expected {replay}")
        kept = {r[0] for r in conn.execute("SELECT transaction_id FROM scored_transactions")}
        counts = {
            t: int(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0])  # type: ignore[index]  # nosec B608
            for t in ("scored_transactions", "alerts", "drift_reports", "fraud_rings")
        }
    if kept != {int(e["TransactionID"]) for e in keep}:
        raise SystemExit(
            "fraud_bench history differs from the raw validation events before the cut"
        )
    log(f"fraud_bench: {counts}, deleted alerts={alerts}; history ids == raw ids before the cut")

    log("starting bench Redis and building the Feast state (batch transactions before the cut)")
    sh([*BENCH, "up", "-d", "--wait", "bench-redis"])
    keys = build_state(cut_dt)

    admin = AdminClient({"bootstrap.servers": KAFKA})
    for f in admin.create_topics(
        [NewTopic(TOPIC, num_partitions=3, replication_factor=1)]
    ).values():
        f.result(timeout=30)
    start_consumers()
    STATE.write_text(
        json.dumps(
            {
                "created": datetime.now(UTC).isoformat(),
                "replay": replay,
                "cut_dt": cut_dt,
                "fraud_bench_counts": counts,
                "redis_keys": keys,
                "replay_ids": replay_ids,
            }
        ),
        encoding="utf-8",
    )
    log("setup done")


def build_state(cut_dt: int) -> int:
    """Feast state in the bench Redis from the batch transactions strictly before ``cut_dt``."""
    if FEAST_DIR.exists():
        shutil.rmtree(FEAST_DIR)
    started = time.monotonic()
    sh(
        [
            sys.executable,
            "-m",
            "src.features.feast_store",
            "materialize",
            "--repo-dir",
            str(FEAST_DIR),
            "--before",
            str(cut_dt),
        ],
        env=_env(FEAST_REDIS="localhost:36379"),
    )
    keys = int(sh([*BENCH, "exec", "-T", "bench-redis", "redis-cli", "dbsize"]).strip())
    log(f"materialized in {time.monotonic() - started:.0f} s; bench Redis keys={keys}")
    return keys


def bench_counts(conn: psycopg.Connection[Any]) -> dict[str, int]:
    tables = ("scored_transactions", "alerts", "drift_reports", "fraud_rings")
    return {
        t: int(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0])  # type: ignore[index]  # nosec B608
        for t in tables
    }


def reset() -> None:
    """Back to the state right after setup: replayed rows removed from fraud_bench, bench Redis
    rebuilt. Used when runs must be repeated from the identical starting point."""
    state = json.loads(STATE.read_text(encoding="utf-8"))
    ids = state["replay_ids"]
    with psycopg.connect(dsn(BENCH_DB)) as conn:
        reviews, alerts, rows = delete_replay_rows(conn, ids)
        counts = bench_counts(conn)
    log(f"removed from fraud_bench: {rows} rows, {alerts} alerts, {reviews} reviews; now {counts}")
    if counts != state["fraud_bench_counts"]:
        raise SystemExit(f"fraud_bench counts {counts} differ from setup")
    sh([*BENCH, "exec", "-T", "bench-redis", "redis-cli", "FLUSHALL"])  # benchmark Redis only
    keys = build_state(state["cut_dt"])
    if keys != state["redis_keys"]:
        raise SystemExit(f"bench Redis keys {keys} differ from setup {state['redis_keys']}")


def _env(**extra: str) -> dict[str, str]:
    import os

    return {**os.environ, **extra}


def start_consumers() -> None:
    log("building the consumer image from this checkout and starting 3 bench consumers")
    sh([*BENCH, "build", "consumer"])
    sh([*BENCH, "up", "-d", "--force-recreate", "consumer"])
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:  # every consumer logs its assignment once it has joined
        logs = [_logs(c) for c in bench_consumers()]
        if len(logs) == 3 and all("partitions assigned: bench-transactions" in t for t in logs):
            log("3 bench consumers joined the group")
            return
        time.sleep(3)
    raise SystemExit("bench consumers did not join the group within 180 s")


def bench_consumers() -> list[str]:
    out = sh(["docker", "ps", "--filter", "name=fraud-bench-consumer", "--format", "{{.Names}}"])
    return sorted(out.split())


def _logs(container: str, since: str | None = None) -> str:
    cmd = ["docker", "logs", *(["--since", since] if since else []), container]
    done = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True)  # nosec B603
    return done.stdout + done.stderr


# --------------------------------------------------------------------------- run


class Watch(threading.Thread):
    """Wall clock every 5 s (suspend check) and docker stats CPU samples, until stopped."""

    def __init__(self, log_file: Path) -> None:
        super().__init__(daemon=True)
        self.log_file, self.done = log_file, threading.Event()
        self.ticks: list[float] = []
        self.cpu: dict[str, list[float]] = defaultdict(list)
        self.stats_thread = threading.Thread(target=self._stats, daemon=True)

    def run(self) -> None:
        self.stats_thread.start()
        with self.log_file.open("w", encoding="utf-8") as fh:
            while not self.done.is_set():
                now = time.time()
                self.ticks.append(now)
                fh.write(f"{datetime.fromtimestamp(now).isoformat()}\n")
                fh.flush()
                self.done.wait(5)
        self.stats_thread.join()

    def _stats(self) -> None:
        while not self.done.is_set():
            out = subprocess.run(  # nosec B603
                ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            for line in out.splitlines():
                name, _, cpu = line.partition("\t")
                if name.startswith(WATCHED) and cpu.endswith("%"):
                    self.cpu[name].append(float(cpu[:-1]))

    def max_gap(self) -> float:
        ticks = [*self.ticks, time.time()]
        return max((b - a for a, b in zip(ticks, ticks[1:], strict=False)), default=0.0)


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, max(0, round(q / 100 * len(s) + 0.5) - 1))], 2)


def summary(values: list[float]) -> dict[str, float | None]:
    return {
        "p50": pct(values, 50),
        "p95": pct(values, 95),
        "p99": pct(values, 99),
        "n": len(values),
    }


PRODUCER = r"""
import base64, json, sys, time
from confluent_kafka import Producer

rate, topic, path = float(sys.argv[1]) or None, sys.argv[2], sys.argv[3]
rows = [json.loads(line) for line in open(path, encoding="utf-8")]
producer = Producer({"bootstrap.servers": "kafka:19092", "acks": "all"})
errors, produced_at = [], []
started = time.monotonic()
for i, r in enumerate(rows):
    produced_at.append((r["id"], time.time()))
    producer.produce(
        topic,
        key=base64.b64decode(r["k"]) if r["k"] is not None else None,
        value=base64.b64decode(r["v"]),
        headers=[("HEADER_NAME", b"HEADER_VALUE")],
        on_delivery=lambda err, _m: errors.append(str(err)) if err else None,
    )
    producer.poll(0)
    if rate:
        ahead = (i + 1) / rate - (time.monotonic() - started)
        if ahead > 0:
            time.sleep(ahead)
undelivered = producer.flush(60)
print(json.dumps({"produced_at": produced_at, "seconds": time.monotonic() - started,
                  "errors": len(errors), "undelivered": undelivered}))
""".replace("HEADER_NAME", SCHEMA_HEADER[0]).replace("HEADER_VALUE", SCHEMA_HEADER[1].decode())


def _b64(data: bytes | None) -> str | None:
    return None if data is None else base64.b64encode(data).decode()


STAGES_RE = re.compile(r"stages id=(\d+) status=(\w+) (\{.*\})")
SCORED_RE = re.compile(r"scored id=(\d+) .* latency_ms=([\d.]+)")


def run(label: str, index: int, limit: int, rate: float | None, timeout: float) -> int:
    state = json.loads(STATE.read_text(encoding="utf-8"))
    replay_ids: list[int] = state["replay_ids"]
    first = VALIDATION_EVENTS - state["replay"] + index
    _, events = stream_events(str(DEFAULT_RAW_DIR), first, limit)
    ids = [int(e["TransactionID"]) for e in events]
    if ids != replay_ids[index : index + limit]:
        raise SystemExit("raw events do not match the replay ids recorded at setup")
    with psycopg.connect(dsn(BENCH_DB)) as conn:
        already = conn.execute(
            "SELECT count(*) FROM scored_transactions WHERE transaction_id = ANY(%s)", (ids,)
        ).fetchone()
    if already and already[0]:
        raise SystemExit(f"{already[0]} of these events are already in fraud_bench")
    consumers = bench_consumers()
    if len(consumers) != 3:
        raise SystemExit(f"expected 3 running bench consumers, found {consumers}")

    OUT.mkdir(parents=True, exist_ok=True)
    events_file = OUT / f"{label}.events.jsonl"
    with events_file.open("w", encoding="utf-8") as fh:  # pre-encoded: encoding is not timed
        for event in events:
            key, value = encode(event)
            rec = {"id": int(event["TransactionID"]), "k": _b64(key), "v": _b64(value)}
            fh.write(json.dumps(rec) + "\n")
    log(f"{label}: replay events {index}..{index + limit - 1} rate={rate or 'unthrottled'}")
    since = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    watch = Watch(OUT / f"{label}.clock.log")
    watch.start()
    time.sleep(6)  # a CPU baseline sample before the first event

    # The producer runs in a container: produce times and scored_at (PostgreSQL) then share the
    # Docker VM clock, which drifts against the Windows host clock by up to ~1 s.
    out = sh(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "fraud-platform_default",
            "-v",
            f"{OUT}:/bench:ro",
            "--entrypoint",
            "python",
            "fraud-bench-consumer:latest",
            "-c",
            PRODUCER,
            str(rate or 0),
            TOPIC,
            f"/bench/{events_file.name}",
        ]
    )
    produced = json.loads(out.strip().splitlines()[-1])
    if produced["errors"] or produced["undelivered"]:
        raise SystemExit(f"producer: {produced['errors']} failed, {produced['undelivered']} queued")
    produced_at = {int(i): float(ts) for i, ts in produced["produced_at"]}
    log(f"produced {len(produced_at)} events in {produced['seconds']:.1f} s (in a container)")

    deadline = time.monotonic() + timeout
    with psycopg.connect(dsn(BENCH_DB), autocommit=True) as conn:
        while True:
            row = conn.execute(
                "SELECT count(*) FROM scored_transactions WHERE transaction_id = ANY(%s)", (ids,)
            ).fetchone()
            stored = int(row[0]) if row else 0
            if stored == len(ids) or time.monotonic() > deadline:
                break
            time.sleep(1)
        rows = conn.execute(
            "SELECT transaction_id, extract(epoch FROM scored_at) FROM scored_transactions "
            "WHERE transaction_id = ANY(%s)",
            (ids,),
        ).fetchall()
    time.sleep(3)  # last stages lines and a CPU sample after the last event
    watch.done.set()
    watch.join()

    scored_at = {int(t): float(s) for t, s in rows}
    e2e = [(scored_at[t] - produced_at[t]) * 1000 for t in scored_at]
    span = max(scored_at.values()) - min(scored_at.values()) if len(scored_at) > 1 else 0.0
    wall = max(scored_at.values()) - min(produced_at.values()) if scored_at else 0.0

    id_set, scorer_ms = set(ids), []
    stages: defaultdict[str, list[float]] = defaultdict(list)
    statuses: defaultdict[str, int] = defaultdict(int)
    for c in consumers:
        for line in _logs(c, since).splitlines():
            if m := STAGES_RE.search(line):
                if int(m.group(1)) in id_set:
                    statuses[m.group(2)] += 1
                    for k, v in json.loads(m.group(3)).items():
                        stages[k].append(float(v))
            elif (m := SCORED_RE.search(line)) and int(m.group(1)) in id_set:
                scorer_ms.append(float(m.group(2)))
    gap = watch.max_gap()
    result = {
        "label": label,
        "replay_index": [index, index + limit - 1],
        "rate": rate,
        "events": len(ids),
        "stored": len(scored_at),
        "statuses": dict(statuses),
        "throughput_events_per_s_first_to_last_scored": round(len(scored_at) / span, 2)
        if span
        else None,
        "throughput_events_per_s_first_produce_to_last_scored": round(len(scored_at) / wall, 2)
        if wall
        else None,
        "e2e_latency_ms": summary(e2e),
        "scorer_latency_ms": summary(scorer_ms),
        "stages_ms": {k: summary(v) for k, v in sorted(stages.items())},
        "cpu_percent_mean": {k: round(statistics.mean(v), 1) for k, v in sorted(watch.cpu.items())},
        "cpu_percent_max": {k: round(max(v), 1) for k, v in sorted(watch.cpu.items())},
        "cpu_samples": {k: len(v) for k, v in sorted(watch.cpu.items())},
        "producer_seconds": produced["seconds"],
        "max_wall_clock_gap_s": round(gap, 1),
        "discarded": gap > 30,
    }
    (OUT / f"{label}.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    if result["discarded"]:
        log(f"DISCARD: wall-clock gap {gap:.0f} s (> 30 s, laptop suspend?)")
        return 2
    return 0 if len(scored_at) == len(ids) else 1


# --------------------------------------------------------------------------- teardown


def teardown() -> None:
    sh([*BENCH, "down", "-v"])  # bench consumers + bench Redis and its volume (this project only)
    admin = AdminClient({"bootstrap.servers": KAFKA})
    existing = set(admin.list_topics(timeout=30).topics)
    doomed = [t for t in (TOPIC, DLQ) if t in existing]
    assert all(t.startswith("bench-") for t in doomed)  # noqa: S101
    for f in admin.delete_topics(doomed).values() if doomed else []:
        f.result(timeout=30)
    groups = {g.group_id for g in admin.list_consumer_groups().result(timeout=30).valid}
    if GROUP in groups:
        for f in admin.delete_consumer_groups([GROUP]).values():
            f.result(timeout=30)
    drop_bench_db()
    if FEAST_DIR.exists():
        shutil.rmtree(FEAST_DIR)
    log(
        f"removed: topics {doomed}, group {GROUP if GROUP in groups else '-'}, "
        f"{BENCH_DB}, bench Redis"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_setup = sub.add_parser("setup")
    p_setup.add_argument("--replay", type=int, default=4000)
    p_run = sub.add_parser("run")
    p_run.add_argument("--label", required=True)
    p_run.add_argument("--index", type=int, required=True, help="first replay event (0-based)")
    p_run.add_argument("--limit", type=int, default=1000)
    p_run.add_argument("--rate", type=float, default=None)
    p_run.add_argument("--timeout", type=float, default=1800)
    sub.add_parser("restart")
    sub.add_parser("reset")
    sub.add_parser("teardown")
    args = parser.parse_args(argv)
    if args.command == "setup":
        setup(args.replay)
    elif args.command == "run":
        return run(args.label, args.index, args.limit, args.rate, args.timeout)
    elif args.command == "restart":
        start_consumers()
    elif args.command == "reset":
        reset()
        start_consumers()
    else:
        teardown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
