"""Stream validation-period IEEE-CIS transactions to Kafka in ``TransactionDT`` order (demo).

Usage: ``python -m src.streaming.producer --skip 0 --limit 1000 [--rate 200]``
(env: ``KAFKA_BOOTSTRAP_SERVERS``, ``KAFKA_TOPIC``). Labels and D1 are never sent.

Only transactions at or after the validation cut-off (``split.validation_start``, the last 20%
of time) are streamed: everything earlier is the training history that the model saw and that
Feast is materialized from. ``--skip`` counts from the cut-off. ``publish`` refuses any event
before the cut-off or out of time order.
"""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Iterable
from typing import Any

from confluent_kafka import Producer

from src.data.load import DEFAULT_RAW_DIR, KEY, load_ieee_cis
from src.features.history import TIME
from src.features.split import validation_start
from src.serving.config import Settings
from src.streaming.events import SCHEMA_HEADER, encode, row_to_event

logger = logging.getLogger(__name__)


class StreamOrderError(ValueError):
    """An event before the validation cut-off or out of time order."""


def stream_events(raw_dir: str, skip: int, limit: int) -> tuple[int, list[dict[str, Any]]]:
    """``(cut-off, events)``: validation-period transactions in (TransactionDT, ID) order."""
    df = load_ieee_cis(raw_dir, usecols=[])
    start = validation_start(df[TIME])
    df = df[df[TIME] >= start].sort_values([TIME, KEY], kind="stable")
    rows = df.iloc[skip : skip + limit].to_dict(orient="records")
    return start, [row_to_event({str(k): v for k, v in r.items()}) for r in rows]


def check_order(events: Iterable[dict[str, Any]], not_before: int) -> Iterable[dict[str, Any]]:
    """Pass events through, refusing any before ``not_before`` or earlier than its predecessor."""
    last = not_before
    for event in events:
        t = int(event[TIME])
        if t < not_before:
            raise StreamOrderError(f"TransactionDT {t} is before the stream cut-off {not_before}")
        if t < last:
            raise StreamOrderError(f"TransactionDT {t} is out of order (previous {last})")
        last = t
        yield event


def publish(
    producer: Producer,
    topic: str,
    events: Iterable[dict[str, Any]],
    rate: float | None = None,
    *,
    not_before: int | None = None,
) -> int:
    sent, errors = 0, []

    def on_delivery(err: Any, _msg: Any) -> None:
        if err is not None:
            errors.append(str(err))

    started = time.monotonic()
    if not_before is not None:
        events = check_order(events, not_before)
    for event in events:
        key, value = encode(event)
        producer.produce(
            topic, key=key, value=value, headers=[SCHEMA_HEADER], on_delivery=on_delivery
        )
        producer.poll(0)
        sent += 1
        if rate:
            lag = sent / rate - (time.monotonic() - started)
            if lag > 0:
                time.sleep(lag)
    # flush() returns how many messages are still queued; their delivery callbacks have not
    # fired, so they would otherwise be lost without an error.
    undelivered = producer.flush(30)
    if undelivered:
        raise RuntimeError(f"{undelivered} of {sent} messages still undelivered after flush")
    if errors:
        raise RuntimeError(f"{len(errors)} messages failed delivery; first: {errors[0]}")
    logger.info("published events=%d topic=%s", sent, topic)
    return sent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    parser.add_argument("--skip", type=int, default=0)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--rate", type=float, default=None, help="events per second")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    producer = Producer({"bootstrap.servers": settings.kafka_bootstrap_servers, "acks": "all"})
    start, events = stream_events(args.raw_dir, args.skip, args.limit)
    logger.info("streaming validation period from TransactionDT %d events=%d", start, len(events))
    return publish(producer, settings.kafka_topic, events, args.rate, not_before=start)


if __name__ == "__main__":
    main()
