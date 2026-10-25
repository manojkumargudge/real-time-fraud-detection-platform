"""Chaos test: restart the TEST stack's Kafka broker in the middle of a replay (demo, manual).

Usage (from the repo root, test stack running: ``docker compose -f docker-compose.test.yml up
-d --wait``)::

    python -m scripts.chaos_kafka_restart [--events 500] [--consumers 3] [--mode restart|kill]

Only the throwaway stack in docker-compose.test.yml (project ``fraud-platform-test``) is ever
touched: its Kafka (localhost:29092) is restarted, and the run writes to a separate database
``chaos`` in its PostgreSQL (localhost:25432), dropped and recreated at the start of each run.
Each run uses its own topic, DLQ topic and consumer group, so runs are repeatable.

Steps:
1. Recreate the ``chaos`` database and its schema; create a 3-partition topic.
2. Start ``--consumers`` host processes of ``python -m src.streaming.consumer``
   (``FEATURE_SOURCE=store``, so the test Redis is not needed).
3. Replay the first ``--events`` validation-period transactions (``src.streaming.producer``) at
   ``--rate`` events/s.
4. When a third of the rows are written, restart the Kafka container and wait until it is
   healthy. ``--mode kill`` kills and starts it instead (forces log recovery); ``--mode pause``
   freezes it for ``--pause-seconds`` (an unresponsive broker: requests time out).
5. Wait until every event is stored, then check: all consumers still running, rows written ==
   events sent, the same transaction IDs, no duplicate IDs, empty DLQ, consumer lag. The
   consumers then exit on their idle timeout and their final counts (``commit_rejected``,
   ``commit_retries``, ``duplicate``) are summed.

Logs and ``summary.json`` go to ``reports/chaos/<run id>/`` (gitignored). Exit code 0 = passed.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess  # nosec B404 - fixed docker/python commands, no shell, no user input
import sys
import threading
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from confluent_kafka import Consumer, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic

from src.data.load import DEFAULT_RAW_DIR
from src.serving.store import PostgresStore
from src.streaming.producer import publish, stream_events

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "docker-compose.test.yml")]
# Test-only stack and credentials (docker-compose.test.yml); never the demo stack.
BOOTSTRAP = "localhost:29092"
ADMIN_DSN = "postgresql://fraud:test-only@localhost:25432/fraud"
CHAOS_DSN = "postgresql://fraud:test-only@localhost:25432/chaos"
PARTITIONS = 3


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


def compose(*args: str) -> str:
    done = subprocess.run([*COMPOSE, *args], check=True, capture_output=True, text=True)  # nosec B603
    return done.stdout


def kafka_health() -> str:
    rows = [json.loads(line) for line in compose("ps", "--format", "json", "kafka").splitlines()]
    return str(rows[0].get("Health") or rows[0].get("State")) if rows else "missing"


def wait_kafka_healthy(timeout: float) -> float:
    started = time.monotonic()
    while (health := kafka_health()) != "healthy":
        if time.monotonic() - started > timeout:
            raise TimeoutError(f"test Kafka not healthy after {timeout:.0f} s ({health})")
        time.sleep(2)
    return time.monotonic() - started


def reset_database() -> None:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS chaos WITH (FORCE)")
        conn.execute("CREATE DATABASE chaos")
    PostgresStore(CHAOS_DSN).close()  # schema once, before the consumers race to create it


def create_topics(*topics: tuple[str, int]) -> None:
    admin = AdminClient({"bootstrap.servers": BOOTSTRAP})
    futures = admin.create_topics(
        [NewTopic(t, num_partitions=n, replication_factor=1) for t, n in topics]
    )
    for future in futures.values():
        future.result(timeout=30)


def stored_rows(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute("SELECT count(*) FROM scored_transactions").fetchone()
    return int(row[0]) if row else 0


def kafka_offsets(topic: str, group: str) -> dict[int, tuple[int, int]]:
    """``{partition: (committed offset, end offset)}`` for ``group`` on ``topic``."""
    probe = Consumer(
        {"bootstrap.servers": BOOTSTRAP, "group.id": group, "enable.auto.commit": False}
    )
    try:
        committed = probe.committed(
            [TopicPartition(topic, p) for p in range(PARTITIONS)], timeout=30
        )
        return {
            tp.partition: (tp.offset, probe.get_watermark_offsets(tp, timeout=30)[1])
            for tp in committed
        }
    finally:
        probe.close()


def topic_size(topic: str) -> int:
    probe = Consumer({"bootstrap.servers": BOOTSTRAP, "group.id": "chaos-probe"})
    try:
        meta = probe.list_topics(topic, timeout=30).topics[topic]
        return sum(
            hi - lo
            for lo, hi in (
                probe.get_watermark_offsets(TopicPartition(topic, p), timeout=30)
                for p in meta.partitions
            )
        )
    finally:
        probe.close()


def final_counts(log_file: Path) -> dict[str, int] | None:
    for line in reversed(log_file.read_text(encoding="utf-8", errors="replace").splitlines()):
        if "consumer stopped counts=" in line:
            parsed = ast.literal_eval(line.split("consumer stopped counts=", 1)[1])
            return {str(k): int(v) for k, v in parsed.items()}
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--events", type=int, default=500)
    parser.add_argument("--skip", type=int, default=0, help="first validation event to replay")
    parser.add_argument("--consumers", type=int, default=3)
    parser.add_argument("--rate", type=float, default=25.0, help="producer events per second")
    parser.add_argument("--mode", choices=["restart", "kill", "pause"], default="restart")
    parser.add_argument(
        "--pause-seconds", type=float, default=70.0, help="--mode pause: how long to freeze"
    )
    parser.add_argument(
        "--idle-timeout", type=float, default=90.0, help="consumer exit after idle s"
    )
    parser.add_argument("--timeout", type=float, default=900.0, help="give up after s")
    args = parser.parse_args(argv)
    if args.mode == "pause":  # no message arrives while frozen: do not exit on idle meanwhile
        args.idle_timeout = max(args.idle_timeout, args.pause_seconds + 120)

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    topic, group = f"chaos-{run_id}", f"chaos-scorer-{run_id}"
    dlq = f"{topic}.dlq"
    out = ROOT / "reports" / "chaos" / run_id
    out.mkdir(parents=True, exist_ok=True)
    log(f"run {run_id}: topic={topic} group={group} logs={out}")

    log(f"test Kafka: {kafka_health()}")
    wait_kafka_healthy(120)
    reset_database()
    create_topics((topic, PARTITIONS))  # the consumers create the DLQ topic, as in production
    _, events = stream_events(str(DEFAULT_RAW_DIR), args.skip, args.events)
    sent_ids = {int(e["TransactionID"]) for e in events}
    log(f"loaded {len(events)} validation events ({len(sent_ids)} unique ids)")

    env = {
        **os.environ,
        "KAFKA_BOOTSTRAP_SERVERS": BOOTSTRAP,
        "KAFKA_TOPIC": topic,
        "KAFKA_GROUP_ID": group,
        "KAFKA_DLQ_TOPIC": dlq,
        "DATABASE_URL": CHAOS_DSN,
        "FEATURE_SOURCE": "store",
        "MODEL_DIR": str(ROOT / "artifacts" / "baseline"),
        "PYTHONUNBUFFERED": "1",
    }
    env.pop("FEAST_REDIS", None)
    logs = [out / f"consumer-{i + 1}.log" for i in range(args.consumers)]
    procs = []
    for path in logs:
        with path.open("w", encoding="utf-8") as fh:
            cmd = [
                sys.executable,
                "-m",
                "src.streaming.consumer",
                "--idle-timeout",
                str(args.idle_timeout),
            ]
            procs.append(
                subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=fh, stderr=subprocess.STDOUT)  # nosec B603
            )
    time.sleep(10)  # let the consumers join the group before the first event

    produced: dict[str, Any] = {}

    def produce() -> None:
        producer = Producer(
            {"bootstrap.servers": BOOTSTRAP, "acks": "all", "enable.idempotence": True}
        )
        try:
            produced["sent"] = publish(producer, topic, events, args.rate)
        except Exception as exc:  # reported in the summary
            produced["error"] = repr(exc)

    producer_thread = threading.Thread(target=produce, daemon=True)
    producer_thread.start()
    log(f"replaying {len(events)} events at {args.rate:g}/s with {args.consumers} consumers")

    restart_at = max(1, len(events) // 3)
    restart: dict[str, Any] = {}
    deadline = time.monotonic() + args.timeout
    with psycopg.connect(CHAOS_DSN, autocommit=True) as conn:
        while True:
            rows = stored_rows(conn)
            if not restart and rows >= restart_at:
                log(f"{rows} rows stored -> {args.mode} test Kafka")
                t0 = time.monotonic()
                if args.mode == "kill":
                    compose("kill", "kafka")
                    compose("start", "kafka")
                elif args.mode == "pause":  # broker frozen, sockets open: requests time out
                    # The producer's final flush waits only 30 s: let it finish first.
                    producer_thread.join()
                    compose("pause", "kafka")
                    time.sleep(args.pause_seconds)
                    compose("unpause", "kafka")
                else:
                    compose("restart", "kafka")
                restart = {"mode": args.mode, "rows_before": rows}
                restart["healthy_after_s"] = round(
                    time.monotonic() - t0 + wait_kafka_healthy(300), 1
                )
                log(f"test Kafka healthy again after {restart['healthy_after_s']} s")
            if rows >= len(sent_ids) and not producer_thread.is_alive():
                break
            if time.monotonic() > deadline:
                log("timeout waiting for all rows")
                break
            if all(p.poll() is not None for p in procs):
                log("all consumers exited before every row was stored")
                break
            time.sleep(1)
        alive = [p.poll() is None for p in procs]
        rows = stored_rows(conn)
        ids = {int(r[0]) for r in conn.execute("SELECT transaction_id FROM scored_transactions")}
        dup_row = conn.execute(
            "SELECT count(*) FROM (SELECT transaction_id FROM scored_transactions "
            "GROUP BY transaction_id HAVING count(*) > 1) d"
        ).fetchone()
        duplicates = int(dup_row[0]) if dup_row else -1
    log(f"stored {rows}/{len(sent_ids)}; consumers alive: {sum(alive)}/{len(procs)}")

    time.sleep(5)  # last commits
    offsets = kafka_offsets(topic, group)
    lag = sum(end - max(committed, 0) for committed, end in offsets.values())
    dlq_messages = topic_size(dlq)

    log(f"waiting up to {args.idle_timeout + 30:.0f} s for the consumers to exit on idle")
    for p in procs:
        try:
            p.wait(timeout=args.idle_timeout + 30)
        except subprocess.TimeoutExpired:
            p.kill()
    exit_codes = [p.returncode for p in procs]
    per_consumer = [final_counts(path) for path in logs]
    totals: Counter[str] = Counter()
    for counts in per_consumer:
        totals.update(
            {k: v for k, v in (counts or {}).items() if k != "consecutive_commit_failures"}
        )
    tracebacks = sum(
        path.read_text(encoding="utf-8", errors="replace").count("Traceback") for path in logs
    )

    summary = {
        "run_id": run_id,
        "topic": topic,
        "group": group,
        "restart": restart,
        "events_sent": produced.get("sent"),
        "producer_error": produced.get("error"),
        "rows_written": rows,
        "ids_match": ids == sent_ids,
        "duplicate_transaction_ids": duplicates,
        "consumers_alive_at_end_of_stream": f"{sum(alive)}/{len(procs)}",
        "consumer_exit_codes": exit_codes,
        "tracebacks_in_logs": tracebacks,
        "consumer_lag": lag,
        "committed_vs_end": {str(p): list(v) for p, v in sorted(offsets.items())},
        "dlq_messages": dlq_messages,
        "commit_rejected": totals["commit_rejected"],
        "commit_retries": totals["commit_retries"],
        "redelivered_duplicates_skipped": totals["duplicate"],
        "consumer_totals": dict(totals),
        "per_consumer": per_consumer,
    }
    passed = (
        bool(restart)
        and all(alive)
        and produced.get("sent") == len(events)
        and rows == len(sent_ids)
        and ids == sent_ids
        and duplicates == 0
        and dlq_messages == 0
        and tracebacks == 0
    )
    summary["passed"] = passed
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    log("PASSED" if passed else "FAILED")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
