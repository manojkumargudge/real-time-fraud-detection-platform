"""Exercise the VM guard without making any GCP calls."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().parents[1] / "deploy" / "with-capacity-vm.sh"


@unittest.skipUnless(
    sys.platform.startswith("linux") and shutil.which("flock"), "Linux VM guard utilities"
)
class TestCapacityGuard(unittest.TestCase):
    def test_stops_after_completion_failure_timeout_and_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            state = directory / "state"
            stops = directory / "stops"
            gcloud = directory / "gcloud"
            gcloud.write_text(
                r"""#!/bin/bash
set -eu
case "$3" in
  describe)
    case "${*: -1}" in
      *value\(id\)*) echo 938503370005181237;;
      *value\(status\)*) cat "$FAKE_STATE";;
      *metadata.items*) echo enable-oslogin;;
      *instanceTerminationAction*) echo STOP;;
      *maxRunDuration.seconds*) echo 14400;;
    esac ;;
  start) echo RUNNING > "$FAKE_STATE";;
  stop) echo TERMINATED > "$FAKE_STATE"; echo stop >> "$FAKE_STOPS";;
  *) exit 1;;
esac
"""
            )
            timeout = directory / "timeout"
            timeout.write_text(
                """#!/bin/bash
if [[ $1 != --foreground ]]; then
  shift
  exec "$@"
fi
case "$FAKE_SCENARIO" in
  success) exit 0;;
  failure) exit 7;;
  timeout) exit 124;;
  interrupted) kill -TERM "$PPID"; exit 143;;
esac
"""
            )
            gcloud.chmod(0o700)
            timeout.chmod(0o700)
            env = {
                **os.environ,
                "XDG_RUNTIME_DIR": root,
                "FAKE_STATE": str(state),
                "FAKE_STOPS": str(stops),
                "PATH": f"{root}:{os.environ['PATH']}",
                "FRAUD_CAPACITY_START_APPROVED": "938503370005181237",
            }
            state.write_text("TERMINATED\n")
            for scenario, expected in (
                ("success", 0),
                ("failure", 7),
                ("timeout", 124),
                ("interrupted", 143),
            ):
                with self.subTest(scenario=scenario):
                    result = subprocess.run(
                        ["bash", str(GUARD), "true"],
                        env={**env, "FAKE_SCENARIO": scenario},
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertEqual(state.read_text().strip(), "TERMINATED")
            self.assertEqual(stops.read_text().splitlines(), ["stop"] * 4)
