"""Independently verify an isolated synthetic, seed-disjoint capacity cohort."""

import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np
import psycopg
from redis import Redis

from src.features.feast_checkpoint import verify_generation
from src.features.feast_store import FeastFeatures, open_store
from src.models.compare import build_model_frame
from src.serving.model import ModelBundle
from src.serving.online_features import entity_keys, events_to_frame
from src.serving.store import PostgresStore

expected_http = int(sys.argv[1])
baseline = sys.argv[2:] == ["baseline"]
expected_kafka = 27000
bundle = ModelBundle.load(Path(os.environ["MODEL_DIR"]))
seed = json.loads(Path(os.environ["FEAST_SEED_ARTIFACT"]).read_bytes())
seed_cards = {card for _, card, _ in seed["events"] if card}
seed_uids = {uid for _, _, uid in seed["events"] if uid}
for card1 in (*range(20000, 20256), *range(30000, 31024)):
    payload = {
        "TransactionID": 1,
        "TransactionDT": 13200000,
        "TransactionAmt": 5,
        "card1": card1,
        "card4": "visa",
        "card6": "debit",
        "addr1": 315,
    }
    if card1 >= 30000:
        payload.update(card2=100, card3=150, card5=226)
    card, uid = entity_keys(payload)
    assert card not in seed_cards and uid not in seed_uids
del seed, seed_cards, seed_uids
if sys.argv[2:] == ["preflight"]:
    print("The synthetic workload is disjoint from the feature seed")
    raise SystemExit(0)
with psycopg.connect(
    os.environ["DATABASE_URL"], options="-c default_transaction_read_only=on"
) as db:
    rows = db.execute(
        "SELECT s.event,s.score,s.is_alert,a.transaction_id IS NOT NULL,s.model_version "
        "FROM scored_transactions s LEFT JOIN alerts a USING(transaction_id) "
        "ORDER BY s.transaction_id"
    ).fetchall()
    if not baseline:
        generation, seed_hash = db.execute(
            "SELECT generation,seed_sha256 FROM feast_checkpoint"
        ).fetchone()
        entity_count = db.execute("SELECT count(*) FROM feast_entity_digests").fetchone()[0]
redis_endpoint = os.environ["FEAST_REDIS"]
if baseline:
    entity_count = None
else:
    assert seed_hash == "c044e3a5d1fedeb7cd173d4c165af676dfa7e1e6f895ae3247f5bb34e1a35f8e"
    online = open_store(
        Path(os.environ["FEAST_REPO_DIR"]) / generation,
        redis=redis_endpoint,
        config=bundle.history_config,
        project=generation,
    )
    store = PostgresStore(os.environ["DATABASE_URL"])
    try:
        verify_generation(
            store, FeastFeatures(online, bundle.history_config), generation, redis_endpoint
        )
    finally:
        store.close()
with Redis.from_url(f"redis://{redis_endpoint}") as redis:
    assert redis.config_get("appendfsync")["appendfsync"] == "always"
    assert redis.info("persistence")["aof_last_write_status"] == "ok"
events = [row[0] for row in rows]
assert len(events) == expected_http + expected_kafka
assert len({event["TransactionID"] for event in events}) == len(events)
assert sum(event["TransactionID"] >= 200_000_000 for event in events) == expected_kafka
assert all(event["TransactionDT"] in (13200000, 13200001) for event in events)
mat = build_model_frame(events_to_frame(events), "baseline", bundle.history_config)
predictions = bundle.predict(mat)
assert list(mat["TransactionID"]) == [event["TransactionID"] for event in events]
digest = hashlib.sha256()
max_score_delta = 0.0
score_differences = 0
for row, expected in zip(rows, predictions, strict=True):
    event, score, decision, alert_exists, version = row
    expected = float(expected)
    max_score_delta = max(max_score_delta, abs(score - expected))
    score_differences += score != expected
    assert decision == (expected >= bundle.threshold) == alert_exists
    assert version == bundle.model_version
    digest.update(f"{event['TransactionID']}:{expected:.9g}:{int(decision)}\n".encode())
print(
    json.dumps(
        {
            "rows": len(rows),
            "http": expected_http,
            "kafka": expected_kafka,
            "alerts": int(np.sum(predictions >= bundle.threshold)),
            "max_score_delta": max_score_delta,
            "score_differences": score_differences,
            "oracle_sha256": digest.hexdigest(),
            "redis_digest_rows_verified": entity_count,
        }
    )
)
assert score_differences == 0
