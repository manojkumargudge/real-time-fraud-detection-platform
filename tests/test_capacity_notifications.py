"""Pub/Sub payload decoding for the private capacity alert verifier."""

import base64
import json
import runpy
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "verify-capacity-notifications.py"
_INCIDENT_FROM = runpy.run_path(str(_SCRIPT))["incident_from"]


def test_decodes_structured_json_and_wrapped_base64_incidents() -> None:
    incident = {"incident_id": "test", "state": "open"}
    payload = {"incident": incident}
    json_payload = json.dumps(payload)
    wrapped = base64.encodebytes(json_payload.encode()).decode()

    for data in (payload, json_payload, wrapped):
        assert _INCIDENT_FROM({"message": {"data": data}}) == incident


@pytest.mark.parametrize("data", [None, "not a Pub/Sub incident!", "eA==", '{"incident": []}', []])
def test_unrelated_or_malformed_payload_does_not_stop_collection(data: object) -> None:
    assert _INCIDENT_FROM({"message": {"data": data}}) == {}
