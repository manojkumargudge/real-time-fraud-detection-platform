"""Kafka consumer: validate -> score -> persist -> commit offset.

Usage: ``python -m src.streaming.consumer [--max-messages N] [--idle-timeout S]``
(env: ``KAFKA_BOOTSTRAP_SERVERS``, ``KAFKA_TOPIC``, ``KAFKA_GROUP_ID``, ``MODEL_DIR``,
``DATABASE_URL``). See ``processor.py`` for delivery semantics.
"""

from __future__ import annotations

import argparse
import logging
import time
from collections import Counter

from confluent_kafka import Consumer, KafkaError, KafkaException

from src.serving.config import Settings
from src.serving.scorer import Scorer
from src.streaming.processor import process_message

logger = logging.getLogger(__name__)


def build_consumer(settings: Settings) -> Consumer:
    return Consumer(
        {
            "bootstrap.servers": settings.kafka_bootstrap_servers,
            "group.id": settings.kafka_group_id,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )


def run(
    consumer: Consumer,
    scorer: Scorer,
    topic: str,
    *,
    max_messages: int | None = None,
    idle_timeout: float | None = None,
) -> Counter[str]:
    """Consume until ``max_messages`` processed or no message for ``idle_timeout`` seconds."""
    counts: Counter[str] = Counter()
    consumer.subscribe([topic])
    last_message = time.monotonic()
    try:
        while max_messages is None or sum(counts.values()) < max_messages:
            msg = consumer.poll(1.0)
            if msg is None:
                if idle_timeout is not None and time.monotonic() - last_message > idle_timeout:
                    break
                continue
            if msg.error():
                code = msg.error().code()
                if code == KafkaError._PARTITION_EOF:
                    continue
                if code == KafkaError.UNKNOWN_TOPIC_OR_PART:  # created on first produce
                    logger.warning("topic not available yet topic=%s; waiting", topic)
                    continue
                raise KafkaException(msg.error())
            last_message = time.monotonic()
            outcome = process_message(msg.value(), scorer)
            consumer.commit(message=msg, asynchronous=False)  # only after the DB write
            counts[outcome.status] += 1
            if outcome.is_alert and outcome.status == "scored":
                counts["alerts"] += 1
    finally:
        consumer.close()
    logger.info("consumer stopped counts=%s", dict(counts))
    return counts


def main(argv: list[str] | None = None) -> Counter[str]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-messages", type=int, default=None)
    parser.add_argument("--idle-timeout", type=float, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    scorer = Scorer(settings.load_bundle(), settings.build_store())
    return run(
        build_consumer(settings),
        scorer,
        settings.kafka_topic,
        max_messages=args.max_messages,
        idle_timeout=args.idle_timeout,
    )


if __name__ == "__main__":
    main()
