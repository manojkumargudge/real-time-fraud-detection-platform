# ruff: noqa: E402
import hashlib
import json
import os
import resource
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import local

sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
import socket
import threading
import urllib.request

import numpy as np
import psycopg
import redis
import uvicorn


@contextmanager
def http_client(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
        thread.start()
        try:
            while not server.started and thread.is_alive():
                time.sleep(0.01)
            assert server.started

            class Response:
                def __init__(self, response):
                    self.body = response.read()
                    self.status_code = response.code

                def json(self):
                    return json.loads(self.body)

            class Client:
                def post(self, path, headers, json):
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}{path}",
                        data=globals()["json"].dumps(json).encode(),
                        headers={**headers, "Content-Type": "application/json"},
                    )
                    try:
                        with urllib.request.urlopen(req, timeout=30) as response:
                            return Response(response)
                    except urllib.error.HTTPError as err:
                        with err:
                            return Response(err)

            yield Client()
        finally:
            server.should_exit = True
            thread.join()


from psycopg.conninfo import make_conninfo

from src.features.feast_store import FeastFeatures, open_store
from src.serving.api import create_app
from src.serving.model import ModelBundle
from src.serving.scorer import Scorer
from src.serving.store import PostgresStore

DB = os.environ["TEST_DATABASE_URL"]
REDIS = redis.Redis.from_url("redis://" + os.environ["TEST_REDIS"])
KEY = "benchmark-only-api-key-at-least-32-characters"
BUNDLE = ModelBundle.load(Path(os.environ["BENCH_BUNDLE"]).resolve())
CANDIDATE = sys.argv[2] != "baseline"
WARMUP_REQUESTS = 32
MEASURED_REQUESTS = 256


timing = local()


def instrument(cls, name, context=False):
    original = getattr(cls, name)

    def record(start):
        if hasattr(timing, "values"):
            timing.values[name] = timing.values.get(name, 0) + (time.perf_counter() - start) * 1000

    if context:

        @contextmanager
        def wrapped(self, *args, **kwargs):
            start = time.perf_counter()
            with original(self, *args, **kwargs) as value:
                record(start)
                yield value
    else:

        @wraps(original)
        def wrapped(self, *args, **kwargs):
            start = time.perf_counter()
            try:
                return original(self, *args, **kwargs)
            finally:
                record(start)

    setattr(cls, name, wrapped)


if os.getenv("BENCH_PROFILE"):
    from feast.infra.online_stores.redis import RedisOnlineStore
    from feast.infra.passthrough_provider import PassthroughProvider
    from psycopg_pool import ConnectionPool

    prep_rows = PassthroughProvider._prep_rows_to_write_for_ingestion

    @wraps(prep_rows)
    def measured_prep(*args, **kwargs):
        started = time.perf_counter()
        try:
            return prep_rows(*args, **kwargs)
        finally:
            if hasattr(timing, "values"):
                stage = "feast_row_conversion"
                timing.values[stage] = (
                    timing.values.get(stage, 0) + (time.perf_counter() - started) * 1000
                )

    PassthroughProvider._prep_rows_to_write_for_ingestion = staticmethod(measured_prep)
    instrument(RedisOnlineStore, "online_write_batch")
    instrument(ConnectionPool, "getconn")
    old_execute = psycopg.Connection.execute

    def measured_execute(self, query, *args, **kwargs):
        if isinstance(query, str) and query.startswith("SELECT pg_advisory_lock("):
            start = time.perf_counter()
            try:
                return old_execute(self, query, *args, **kwargs)
            finally:
                if hasattr(timing, "values"):
                    timing.values["advisory_lock_roundtrip"] = (time.perf_counter() - start) * 1000
        return old_execute(self, query, *args, **kwargs)

    psycopg.Connection.execute = measured_execute
    if CANDIDATE:
        instrument(PostgresStore, "serialize_card", context=True)
        instrument(PostgresStore, "pending_feast")
        instrument(PostgresStore, "finish_feast")
    for cls, name in [
        (PostgresStore, "get"),
        (PostgresStore, "save"),
        (FeastFeatures, "read"),
        (FeastFeatures, "push"),
        (ModelBundle, "predict"),
    ]:
        instrument(cls, name)


def run_load(clients):
    scope = f"bench_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DB, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {scope}")
    dsn = make_conninfo(
        DB,
        options=f"-c search_path={scope}",
        application_name=f"readiness_{sys.argv[2]}_{clients}",
    )
    pg = PostgresStore(dsn)
    with TemporaryDirectory() as temp:
        opts = {"reject_late": True} if CANDIDATE else {}
        repo = Path(temp)
        if sys.argv[2] == "direct":
            from src.features.feast_checkpoint import promote, seed_metadata

            seed = repo / "seed.json"
            contents = json.dumps({"metadata": seed_metadata(BUNDLE, 0), "events": []}).encode()
            seed.write_bytes(contents)
            scope = promote(
                pg,
                dsn,
                os.environ["TEST_REDIS"],
                repo,
                BUNDLE,
                0,
                seed,
                hashlib.sha256(contents).hexdigest(),
            )
            opts["feast_generation"] = scope
            repo = repo / scope
        fs = open_store(repo, redis=os.environ["TEST_REDIS"], project=scope)
        feast_options = {"serialize_online_rows": sys.argv[2] == "direct"} if CANDIDATE else {}
        feast = FeastFeatures(fs, BUNDLE.history_config, **feast_options)
        scorer = Scorer(BUNDLE, pg, feast, True, **opts)
        api_opts = {"production": True} if CANDIDATE else {}
        profiles = {}
        score_event = scorer.score_event

        def measured_score(event):
            if CANDIDATE:
                from src import timing as application_timing

                application_stages = application_timing.collect()
            timing.values = {}
            try:
                return score_event(event)
            finally:
                if CANDIDATE:
                    timing.values.update(application_stages)
                    application_timing.clear()
                profiles[event.TransactionID] = timing.values

        scorer.score_event = measured_score
        with http_client(create_app(scorer, api_key=KEY, **api_opts)) as api:

            def send(index: int, dt: int = 200_000) -> tuple[float, int, bool]:
                start = time.perf_counter()
                response = api.post(
                    "/score",
                    headers={"X-API-Key": KEY},
                    json={
                        "TransactionID": index + 1,
                        "TransactionDT": dt,
                        "TransactionAmt": 50 + index % 35,
                        "card1": 1111 + index % 16,
                        "card4": "visa",
                        "card6": "debit",
                        "addr1": 315,
                        "ProductCD": "W",
                    },
                )
                body = response.json()
                outcomes[index] = [body.get(key) for key in ("score", "is_alert", "history_rows")]
                return (
                    (time.perf_counter() - start) * 1000,
                    response.status_code,
                    (response.json().get("duplicate") is True),
                )

            outcomes = {}
            for index in range(WARMUP_REQUESTS):
                assert send(index, 100_000)[1] == 200
            REDIS.execute_command("LATENCY", "RESET", "aof-fsync-always")
            started = time.perf_counter()
            cpu_started = time.process_time()
            measured_indices = range(WARMUP_REQUESTS, WARMUP_REQUESTS + MEASURED_REQUESTS)
            with ThreadPoolExecutor(max_workers=clients) as pool:
                results = list(pool.map(send, measured_indices))
            elapsed = time.perf_counter() - started
            successful = [
                latency for latency, status, duplicate in results if status == 200 and not duplicate
            ]
            assert len(successful) == len(results), results
            assert all(outcomes[index][2] >= 1 for index in measured_indices), (
                "Warmup state was not visible"
            )
            measured = [profiles[index + 1] for index in measured_indices]
            stages = {
                stage: round(float(np.median([row.get(stage, 0) for row in measured])), 2)
                for stage in measured[0]
            }
            digest = hashlib.sha256(json.dumps(sorted(outcomes.items())).encode()).hexdigest()
            print(
                json.dumps(
                    {
                        "cpu_seconds": round(time.process_time() - cpu_started, 3),
                        "peak_rss_mib": round(
                            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1
                        ),
                        "timeouts": 0,
                        "redis_fsync_events_ms": REDIS.execute_command(
                            "LATENCY", "HISTORY", "aof-fsync-always"
                        ),
                        "score_digest": digest,
                        "stage_p50_ms": stages,
                        "revision": sys.argv[2],
                        "clients": clients,
                        "requests": len(results),
                        "unique_scored": len(successful),
                        "errors": sum(status != 200 for _, status, _ in results),
                        "p50_ms": round(float(np.percentile(successful, 50)), 2),
                        "p95_ms": round(float(np.percentile(successful, 95)), 2),
                        "p99_ms": round(float(np.percentile(successful, 99)), 2),
                        "throughput": round(len(successful) / elapsed, 2),
                    }
                ),
                flush=True,
            )
        # Keep isolated Redis keys for inspection.
    pg.close()


for clients in (1, 4, 8):
    run_load(clients)
