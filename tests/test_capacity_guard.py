"""Exercise the VM guard without making any GCP calls."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().parents[1] / "deploy" / "with-capacity-vm.sh"
MAINTENANCE = GUARD.with_name("check-capacity-maintenance.sh")


@unittest.skipUnless(
    sys.platform.startswith("linux") and shutil.which("flock"), "Linux VM guard utilities"
)
class TestCapacityGuard(unittest.TestCase):
    def test_waits_through_oneshot_activation_and_timer_reschedule(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            systemctl = directory / "systemctl"
            systemctl.write_text(
                """#!/bin/bash
case "$3" in
  *NextElapseUSecRealtime*)
    if [[ ! -e $MARKER && $2 == apt-daily-upgrade.timer ]]; then
      echo n/a
    else
      date -u -d 'tomorrow' '+%Y-%m-%d %H:%M:%S UTC'
    fi ;;
  *ActiveState*)
    if [[ ! -e $MARKER && $2 == apt-daily-upgrade.service ]]; then
      echo activating
    else
      echo inactive
    fi ;;
  *Result*) echo success;;
esac
"""
            )
            sleep = directory / "sleep"
            sleep.write_text('#!/bin/bash\ntouch "$MARKER"\n')
            dpkg = directory / "dpkg-query"
            dpkg.write_text("#!/bin/bash\nexit 0\n")
            for executable in (systemctl, sleep, dpkg):
                executable.chmod(0o700)
            marker = directory / "finished"
            env = {
                **os.environ,
                "MARKER": str(marker),
                "PATH": f"{root}:{os.environ['PATH']}",
            }
            result = subprocess.run(
                ["bash", str(MAINTENANCE), "5"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(marker.exists())

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
      *metadata.items*) echo "$FAKE_METADATA";;
      *instanceTerminationAction*) echo STOP;;
      *maxRunDuration.seconds*) echo "$FAKE_STOP_SECONDS";;
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
                "FAKE_STOP_SECONDS": "13800",
                "FAKE_METADATA": "enable-oslogin",
            }
            state.write_text("TERMINATED\n")
            for key, value in (
                ("FAKE_STOP_SECONDS", "14400"),
                ("FAKE_METADATA", "enable-oslogin;startup-script-url"),
            ):
                with self.subTest(key=key):
                    denied = subprocess.run(
                        ["bash", str(GUARD), "true"],
                        env={**env, key: value},
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertNotEqual(denied.returncode, 0)
                    self.assertEqual(state.read_text().strip(), "TERMINATED")
                    self.assertFalse(stops.exists())
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
