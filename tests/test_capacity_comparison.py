"""Regression checks for the release's offline sustained comparison."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "compare-capacity-trials.py"
LEVELS = ("spread-1", "spread-4", "spread-8", "hot-8")


class TestCapacityComparison(unittest.TestCase):
    def run_comparison(
        self, *, release_rps: float = 95, include_main: bool = True, seconds: int = 600
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory)
            for group in ("main", "release"):
                if group == "main" and not include_main:
                    continue
                for repeat in (1, 2):
                    trial = session / f"{group}-{repeat}"
                    trial.mkdir()
                    (trial / "complete").touch()
                    for level in LEVELS:
                        row = {
                            "measured_start_unix": 1000,
                            "measured_end_unix": 1000 + (600 if group == "main" else seconds),
                            "errors": 0,
                            "timeouts": 0,
                            "duplicates": 0,
                            "throughput_rps": 100 if group == "main" else release_rps,
                            "p50_ms": 5,
                            "p95_ms": 10,
                            "p99_ms": 15,
                        }
                        (trial / f"{level}.json").write_text(json.dumps([row]))
            return subprocess.run(
                [sys.executable, str(SCRIPT), str(session)],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_accepts_original_throughput_boundary(self) -> None:
        result = self.run_comparison()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["passed"])

    def test_reports_failing_throughput(self) -> None:
        result = self.run_comparison(release_rps=94.9)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)["passed"])

    def test_requires_completed_main_trials(self) -> None:
        result = self.run_comparison(include_main=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Incomplete trial:", result.stderr)

    def test_rejects_short_measurements(self) -> None:
        result = self.run_comparison(seconds=599)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Measurement shorter than 600 seconds:", result.stderr)
