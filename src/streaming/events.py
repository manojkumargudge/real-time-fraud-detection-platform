"""Kafka message format for transaction events (schema v1).

* value: UTF-8 JSON object matching ``src.serving.schema.TransactionEvent`` (no label, no D1)
* key:   the event's ``card_id`` proxy (UTF-8), so one card's events share a partition and
  keep their relative order; events without ``card1`` use the ``TransactionID`` as key.
* header ``schema_version``: ``"1"``
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd

from src.data.load import HAS_IDENTITY
from src.serving.online_features import entity_keys
from src.serving.schema import EVENT_SCHEMA_VERSION, TransactionEvent

SCHEMA_HEADER = ("schema_version", str(EVENT_SCHEMA_VERSION).encode())
EVENT_FIELDS = tuple(TransactionEvent.model_fields)
INT_FIELDS = ("TransactionID", "TransactionDT", "card1")


def row_to_event(row: dict[str, Any]) -> dict[str, Any]:
    """Map a loaded IEEE-CIS row to an event dict (drops label, D1 and all other columns)."""
    event: dict[str, Any] = {}
    for field in EVENT_FIELDS:
        value = row.get(field)
        if value is None or (not isinstance(value, str) and pd.isna(value)):
            event[field] = None
        elif field in INT_FIELDS:
            event[field] = int(value)
        elif field == HAS_IDENTITY:
            event[field] = bool(value)
        elif isinstance(value, str):
            event[field] = value
        else:
            event[field] = float(value)
    event[HAS_IDENTITY] = bool(event.get(HAS_IDENTITY) or False)
    return TransactionEvent.model_validate(event).to_record()


def encode(event: dict[str, Any]) -> tuple[bytes, bytes]:
    """Return ``(key, value)`` bytes for a validated event."""
    validated = TransactionEvent.model_validate(event).to_record()
    card_id, _ = entity_keys(validated)
    key = card_id if card_id is not None else f"id:{validated['TransactionID']}"
    return key.encode(), json.dumps(validated, separators=(",", ":")).encode()


def decode(value: bytes | None) -> TransactionEvent:
    """Parse and validate a message value. Raises ``ValueError`` on any problem."""
    if value is None:
        raise ValueError("empty message value")
    try:
        payload = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"message is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("message JSON must be an object")
    return TransactionEvent.model_validate(payload)
