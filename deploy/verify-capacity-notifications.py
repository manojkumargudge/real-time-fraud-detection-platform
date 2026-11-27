"""Verify opening and recovery messages on the dedicated rehearsal subscription."""

import argparse
import base64
import json
import subprocess
import sys
import time
from datetime import datetime

PROJECT = "fraud-detection-platform-2026"
SUBSCRIPTION = "fraud-capacity-verifier-1127"
INSTANCE = "938503370005181237"


def incident_from(message: dict) -> dict:
    payload = message["message"]["data"]
    if not isinstance(payload, dict):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = json.loads(base64.b64decode(payload, validate=True))
    return payload.get("incident", {})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy-name", required=True, help="Alert policy display name")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--not-before", type=datetime.fromisoformat)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    observed: dict[str, dict[str, str]] = {}
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        try:
            raw = subprocess.run(
                [
                    "gcloud",
                    "pubsub",
                    "subscriptions",
                    "pull",
                    SUBSCRIPTION,
                    "--project",
                    PROJECT,
                    "--limit=10",
                    "--return-immediately",
                    "--auto-ack",
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
        for message in json.loads(raw.stdout):
            if message.get("ackStatus") != "SUCCESS":
                continue
            incident = incident_from(message)
            resource = incident.get("resource") or {}
            labels = resource.get("labels") or {}
            if (
                incident.get("policy_name") != args.policy_name
                or labels.get("instance_id") != INSTANCE
            ):
                continue
            incident_id = incident.get("incident_id")
            state = incident.get("state")
            if not incident_id or state not in {"open", "closed"}:
                continue
            timestamp = message["message"]["publishTime"]
            if args.not_before and datetime.fromisoformat(timestamp) < args.not_before:
                continue
            states = observed.setdefault(incident_id, {})
            if state not in states:
                states[state] = timestamp
                print(
                    json.dumps(
                        {"incident_id": incident_id, "state": state, "published": timestamp}
                    ),
                    flush=True,
                )
            if {"open", "closed"} <= states.keys():
                print(json.dumps({"incident_id": incident_id, "publish_times": states}))
                return
        time.sleep(5)
    raise SystemExit("No matching open and closed notifications received within the time limit")


if __name__ == "__main__":
    main()
