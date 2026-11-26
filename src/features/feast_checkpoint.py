"""Build immutable seed artifacts and promote fully checked Feast namespaces."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from redis import Redis

from src.data.load import load_ieee_cis
from src.features import feast_store as fs
from src.features.entity import CARD_ID, USER_ID
from src.features.history import TIME
from src.serving.model import ModelBundle
from src.serving.store import PostgresStore


def seed_metadata(bundle: ModelBundle, before: int) -> dict[str, Any]:
    return {
        "schema": 1,
        "model_sha256": bundle.metadata["model_sha256"],
        "model_metadata_sha256": hashlib.sha256(
            json.dumps(bundle.metadata, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "model_version": bundle.model_version,
        "cutoff": before,
        "windows": list(map(list, bundle.history_config.windows)),
        "change_columns": list(bundle.history_config.change_columns),
        "feature_columns": bundle.feature_columns,
    }


def load_seed(path: Path, digest: str, bundle: ModelBundle, before: int) -> list[fs.EntityEvent]:
    contents = path.read_bytes()
    if hashlib.sha256(contents).hexdigest() != digest:
        raise ValueError("Feature seed checksum mismatch")
    artifact = json.loads(contents)
    if artifact["metadata"] != seed_metadata(bundle, before):
        raise ValueError("Feature seed is incompatible with the cutoff or model bundle")
    events = [fs.EntityEvent(*row) for row in artifact["events"]]
    ids = {entry.event["TransactionID"] for entry in events}
    if len(ids) != len(events) or any(entry.event[TIME] >= before for entry in events):
        raise ValueError("Feature seed contains duplicate or out-of-range events")
    return events


def verify_generation(
    store: PostgresStore, feast: fs.FeastFeatures, generation: str, redis: str
) -> None:
    with store.feast_maintenance() as conn:
        row = conn.execute("SELECT generation FROM feast_checkpoint").fetchone()
        if row is None or row["generation"] != generation:
            raise ConnectionError("Feature checkpoint changed during startup")
        if conn.execute("SELECT 1 FROM pending_feast_updates LIMIT 1").fetchone() is not None:
            raise ConnectionError("Feature journal must be drained before opening the scorer")
        expected = 0
        for digest in conn.execute(
            "SELECT entity_type, entity_key, digest FROM feast_entity_digests"
        ):
            if feast.entity_digest(digest["entity_type"], digest["entity_key"]) != digest["digest"]:
                raise ConnectionError("Redis generation does not match the PostgreSQL checkpoint")
            expected += 1
        with Redis.from_url(f"redis://{redis}") as client:
            keys = set(client.scan_iter(match=f"*{generation}".encode()))
        if len(keys) != expected:
            raise ConnectionError("Redis generation contains unexpected feature keys")


def promote(
    store: PostgresStore,
    dsn: str,
    redis: str,
    repo: Path,
    bundle: ModelBundle,
    before: int,
    seed: Path,
    seed_digest: str,
) -> str:
    events = load_seed(seed, seed_digest, bundle, before)
    generation = f"fraud_{uuid.uuid4().hex}"
    target = repo / generation
    config = bundle.history_config
    with store.feast_maintenance() as conn:
        previous = conn.execute("SELECT seed_sha256 FROM feast_checkpoint").fetchone()
        if previous is not None and previous["seed_sha256"] != seed_digest:
            raise ValueError("An active checkpoint is bound to a different seed")
        events.extend(fs.durable_feature_events(dsn, before, include_pending=True))
        uids, cards = fs.fold_states(events, config, snapshots=False)
        online = fs.open_store(target, redis=redis, config=config, project=generation)
        fs.write_offline(target, uids, cards, config=config)
        if uids or cards:
            fs.materialize_all(online)
        feast = fs.FeastFeatures(online, config)
        digests = []
        for entity_type, key_field, fields, rows in (
            ("uid", USER_ID, fs.uid_fields(), uids),
            ("card", CARD_ID, fs.card_fields(config.change_columns), cards),
        ):
            for row in rows:
                key = row[key_field]
                expected = fs.online_digest(row, fields)
                if expected is None or feast.entity_digest(entity_type, key) != expected:
                    raise ConnectionError("Rebuilt Redis feature row failed validation")
                digests.append((entity_type, key, expected))
        conn.execute("DELETE FROM feast_entity_digests")
        with conn.cursor() as cursor:
            cursor.executemany("INSERT INTO feast_entity_digests VALUES (%s, %s, %s)", digests)
        conn.execute("DELETE FROM pending_feast_updates")
        conn.execute(
            "INSERT INTO feast_checkpoint (generation, seed_sha256) VALUES (%s, %s) "
            "ON CONFLICT (singleton) DO UPDATE SET generation = EXCLUDED.generation, "
            "seed_sha256 = EXCLUDED.seed_sha256",
            (generation, seed_digest),
        )
    return generation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["seed", "promote"])
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--before", type=int, required=True)
    parser.add_argument("--raw-dir", type=Path)
    parser.add_argument("--repo-dir", type=Path)
    args = parser.parse_args()
    bundle = ModelBundle.load(args.model_dir)
    if args.command == "seed":
        if args.raw_dir is None:
            parser.error("seed requires --raw-dir")
        frame = load_ieee_cis(args.raw_dir, usecols=[])
        events = fs.pre_stream_events(frame, args.before, bundle.history_config)
        contents = json.dumps(
            {
                "metadata": seed_metadata(bundle, args.before),
                "events": [(ev.event, ev.card, ev.uid) for ev in events],
            },
            separators=(",", ":"),
        ).encode()
        with args.artifact.open("xb") as output:
            output.write(contents)
        print(hashlib.sha256(contents).hexdigest())
        return
    dsn = os.environ["DATABASE_URL"]
    if args.repo_dir is None:
        parser.error("promote requires --repo-dir")
    store = PostgresStore(dsn)
    try:
        print(
            promote(
                store,
                dsn,
                os.environ["FEAST_REDIS"],
                args.repo_dir,
                bundle,
                args.before,
                args.artifact,
                os.environ["FEAST_SEED_SHA256"],
            )
        )
    finally:
        store.close()


if __name__ == "__main__":
    main()
