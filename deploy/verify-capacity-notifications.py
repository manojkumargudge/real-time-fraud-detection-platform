"""Verify opening and recovery messages on the dedicated rehearsal subscription."""

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

PROJECT = "fraud-detection-platform-2026"
SUBSCRIPTION = "fraud-capacity-verifier-1127"
INSTANCE = "938503370005181237"


def incident_from(message: dict) -> dict:
    envelope = message.get("message")
    if not isinstance(envelope, dict):
        return {}
    payload = envelope.get("data")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            try:
                decoded = base64.b64decode("".join(payload.split()), validate=True)
                payload = json.loads(decoded)
            except ValueError:
                return {}
    if not isinstance(payload, dict):
        return {}
    incident = payload.get("incident")
    return incident if isinstance(incident, dict) else {}


def save_receipts(path: Path, scope: dict, observed: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump({"scope": scope, "observed": observed}, stream)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def record_receipt(message: dict, scope: dict, observed: dict, path: Path) -> str | None:
    incident = incident_from(message)
    resource = incident.get("resource")
    labels = resource.get("labels") if isinstance(resource, dict) else None
    incident_id, state = incident.get("incident_id"), incident.get("state")
    ack_id = message.get("ackId")
    if (
        not isinstance(ack_id, str)
        or not ack_id
        or not isinstance(incident_id, str)
        or not incident_id
        or state not in ("open", "closed")
        or incident.get("policy_name") != scope["policy"]
        or not isinstance(labels, dict)
        or labels.get("instance_id") != INSTANCE
    ):
        return None
    timestamp = message["message"].get("publishTime")
    try:
        published = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None
    if published.utcoffset() is None or published < datetime.fromisoformat(scope["not_before"]):
        return None
    states = observed.setdefault(incident_id, {})
    if state not in states:
        states[state] = timestamp
        save_receipts(path, scope, observed)
        print(
            json.dumps({"incident_id": incident_id, "state": state, "published": timestamp}),
            flush=True,
        )
    return ack_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy-name", required=True, help="Alert policy display name")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--not-before", type=datetime.fromisoformat, required=True)
    parser.add_argument("--evidence-file", type=Path, required=True)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.not_before.utcoffset() is None:
        parser.error("--not-before must include a UTC offset")
    scope = {"policy": args.policy_name, "not_before": args.not_before.isoformat()}
    observed: dict[str, dict[str, str]] = {}
    if args.evidence_file.exists():
        saved = json.loads(args.evidence_file.read_text())
        if saved["scope"] != scope:
            parser.error("evidence file belongs to another verification window")
        observed = saved["observed"]
    deadline = time.monotonic() + args.timeout
    command = ["gcloud", "pubsub", "subscriptions"]
    while time.monotonic() < deadline:
        for incident_id, states in observed.items():
            if {"open", "closed"} <= states.keys():
                print(json.dumps({"incident_id": incident_id, "publish_times": states}))
                return
        try:
            raw = subprocess.run(
                [
                    *command,
                    "pull",
                    SUBSCRIPTION,
                    "--project",
                    PROJECT,
                    "--limit=10",
                    "--format=json",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=min(60, max(1, deadline - time.monotonic())),
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            print(f"Notification pull retry after {type(error).__name__}", file=sys.stderr)
            time.sleep(min(10, max(0, deadline - time.monotonic())))
            continue
        ack_ids = [
            ack_id
            for message in json.loads(raw.stdout)
            if (ack_id := record_receipt(message, scope, observed, args.evidence_file))
        ]
        if ack_ids:
            try:
                subprocess.run(
                    [
                        *command,
                        "ack",
                        SUBSCRIPTION,
                        "--project",
                        PROJECT,
                        "--ack-ids=" + ",".join(ack_ids),
                    ],
                    capture_output=True,
                    check=True,
                    timeout=10,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                print(
                    "Acknowledgment failed. Durable delivery evidence was retained.",
                    file=sys.stderr,
                )
        else:
            time.sleep(min(5, max(0, deadline - time.monotonic())))
    raise SystemExit("No matching open and closed notifications received within the time limit")


if __name__ == "__main__":
    main()
