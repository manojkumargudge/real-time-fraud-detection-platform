"""Pub/Sub payload decoding for the private capacity alert verifier."""

import base64
import json
import runpy
import subprocess
import sys
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


def test_verifier_records_before_ack_and_resumes_matching_incident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    evidence = tmp_path / "receipts.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(_SCRIPT),
            "--policy-name",
            "test policy",
            "--not-before",
            "2026-11-28T00:00:00Z",
            "--evidence-file",
            str(evidence),
        ],
    )

    def message(incident_id: str, state: str) -> dict:
        return {
            "ackId": f"{incident_id}-{state}",
            "message": {
                "publishTime": "2026-11-28T01:00:00Z",
                "data": {
                    "incident": {
                        "incident_id": incident_id,
                        "state": state,
                        "policy_name": "test policy",
                        "resource": {"labels": {"instance_id": "938503370005181237"}},
                    }
                },
            },
        }

    opening = [message("a", "open"), {"ackId": "malformed", "message": []}]
    closure = [message("other", "closed"), message("a", "closed")]
    acked = []

    def first_run(command: list[str], **_: object) -> subprocess.CompletedProcess:
        if "pull" in command:
            assert "--auto-ack" not in command
            return subprocess.CompletedProcess(command, 0, json.dumps(opening))
        assert command[-1] == "--ack-ids=a-open"
        assert "open" in json.loads(evidence.read_text())["observed"]["a"]
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess, "run", first_run)
    with pytest.raises(KeyboardInterrupt):
        runpy.run_path(str(_SCRIPT), run_name="__main__")

    def second_run(command: list[str], **_: object) -> subprocess.CompletedProcess:
        if "pull" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps(closure))
        acked.append(command[-1])
        assert set(json.loads(evidence.read_text())["observed"]["a"]) == {"open", "closed"}
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", second_run)
    runpy.run_path(str(_SCRIPT), run_name="__main__")
    assert acked == ["--ack-ids=other-closed,a-closed"]
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["incident_id"] == "a"
    assert set(result["publish_times"]) == {"open", "closed"}
