"""Read or seed the synthetic recovery cohort inside the isolated Compose services."""

import hashlib
import json
import os
import sys
from pathlib import Path


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def kafka(action: str) -> None:
    from confluent_kafka import Consumer, Producer, TopicPartition

    from src.streaming.events import SCHEMA_HEADER, encode

    if action == "seed":
        producer = Producer({"bootstrap.servers": "kafka:19092", "enable.idempotence": True})
        errors = []
        for transaction_id in (910000001, 910000002):
            key, value = encode(
                {
                    "TransactionID": transaction_id,
                    "TransactionDT": transaction_id - 910000000 + 100,
                    "TransactionAmt": 50,
                    "ProductCD": "W",
                    "card1": 3999,
                    "card4": "visa",
                    "card6": "debit",
                    "addr1": 315,
                }
            )
            producer.produce(
                "transactions",
                key=key,
                value=value,
                headers=[SCHEMA_HEADER],
                partition=0,
                on_delivery=lambda error, _: errors.append(error) if error is not None else None,
            )
        assert producer.flush(15) == 0 and not errors
        return

    import psycopg
    from redis import Redis

    partition = TopicPartition("transactions", 0)
    with psycopg.connect(
        os.environ["DATABASE_URL"], options="-c default_transaction_read_only=on"
    ) as pg:
        rows = pg.execute("SELECT transaction_id FROM scored_transactions ORDER BY 1").fetchall()
        assert rows == [(910000001,), (910000002,)], rows
        assert pg.execute("SELECT count(*) FROM pending_feast_updates").fetchone() == (0,)
        alerts = pg.execute("SELECT transaction_id FROM alerts ORDER BY 1").fetchall()
        assert alerts == rows, alerts
    client = Consumer(
        {
            "bootstrap.servers": "kafka:19092",
            "group.id": "fraud-scorer",
            "enable.auto.commit": False,
        }
    )
    try:
        offsets = client.committed([partition], timeout=8)
        # Source cohort IDs and offsets are known independently from the two producer sends.
        assert offsets[0].offset == 2
        assert client.get_watermark_offsets(partition, timeout=8) == (0, 2)
        assert client.get_watermark_offsets(TopicPartition("transactions.dlq", 0), timeout=8) == (
            0,
            0,
        )
        client.assign([TopicPartition("transactions", 0, 0)])
        for index, (transaction_id,) in enumerate(rows):
            record = client.poll(8)
            assert record is not None and record.error() is None and record.offset() == index
            assert json.loads(record.value())["TransactionID"] == transaction_id
    finally:
        client.close()
    redis = Redis(host="redis")
    try:
        keys = sorted(redis.scan_iter())
        assert len(keys) == 2
        values = [
            [
                key.hex(),
                sorted((field.hex(), value.hex()) for field, value in redis.hgetall(key).items()),
            ]
            for key in keys
        ]
        assert redis.info("persistence")["aof_last_write_status"] == "ok"
    finally:
        redis.close()
    print(
        json.dumps(
            {
                "committed": 2,
                "lag": 0,
                "scores": 2,
                "alerts": 2,
                "journal": 0,
                "dlq": 0,
                "redis_keys": len(keys),
                "redis_sha256": digest(json.dumps(values).encode()),
            },
            sort_keys=True,
        )
    )


def registry(seed: bool) -> None:
    import tempfile

    from mlflow import MlflowClient

    client = MlflowClient(tracking_uri="http://mlflow:5000", registry_uri="http://mlflow:5000")
    name = "rehearsal-fraud"
    model = Path("/app/artifacts/baseline/model.json")
    if seed:
        experiment = client.create_experiment(name)
        run = client.create_run(experiment)
        run_id = run.info.run_id
        client.log_artifacts(run_id, str(model.parent), artifact_path="bundle")
        client.create_registered_model(name)
        version = client.create_model_version(name, f"{run.info.artifact_uri}/bundle", run_id)
        client.set_registered_model_alias(name, "champion", version.version)
        client.set_terminated(run_id)
    version = client.get_model_version_by_alias(name, "champion")
    with tempfile.TemporaryDirectory() as directory:
        assert version.run_id is not None
        restored = Path(client.download_artifacts(version.run_id, "bundle/model.json", directory))
        assert restored.read_bytes() == model.read_bytes()
        metadata = Path(
            client.download_artifacts(version.run_id, "bundle/metadata.json", directory)
        )
        assert metadata.read_bytes() == model.with_name("metadata.json").read_bytes()
    print(
        json.dumps(
            {
                "alias": "champion",
                "version": version.version,
                "run_id": version.run_id,
                "model_sha256": digest(model.read_bytes()),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    action = sys.argv[1]
    if action.startswith("registry-"):
        registry(action == "registry-seed")
    else:
        kafka(action)
