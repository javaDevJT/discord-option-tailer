"""The pre-checkout storage wait retries only pending job identity binding."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


class StorageStartTests(unittest.TestCase):
    def test_bounded_pending_wait_and_fail_closed_errors(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/ci-release.yml").read_text()
        step = workflow.split("      - name: Start BuildKit storage collection\n", 1)[1].split("      - name:", 1)[0]
        self.assertIn("timeout-minutes: 3", step)
        script = textwrap.dedent(step.split("        run: |\n", 1)[1])
        pending = {
            "efficiency_policy": {"mode": "pending", "reason": "job-context-pending"},
            "client_validation": {"error": "storage policy decision remained pending after 12 bounded start retries"},
        }
        cases = [
            ([], 0, 1),
            ([pending, pending], 0, 3),
            ([pending] * 4, 1, 4),
            ([{"efficiency_policy": {"mode": "denied"}}], 1, 1),
            ([{**pending, "client_validation": {"error": "runner identity mismatch"}}], 1, 1),
            ([None], 1, 1),
            ([pending, None], 1, 2),
            (["malformed"], 1, 1),
        ]
        for failures, expected_code, expected_calls in cases:
            with self.subTest(failures=failures), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "failures.json").write_text(json.dumps(failures))
                command = root / "ci-storage-efficiency"
                command.write_text("#!/usr/bin/env python3\n" + textwrap.dedent('''\
                    import json, os, pathlib, sys
                    root = pathlib.Path(os.environ["RUNNER_TEMP"])
                    calls = root / "calls"
                    count = int(calls.read_text()) + 1 if calls.exists() else 1
                    calls.write_text(str(count))
                    report = root / "ci-storage-usage-start.json"
                    failures = json.loads((root / "failures.json").read_text())
                    if count <= len(failures):
                        failure = failures[count - 1]
                        if failure is not None:
                            report.write_text(failure if isinstance(failure, str) else json.dumps(failure))
                        sys.exit(1)
                    sys.exit(0)
                '''))
                command.chmod(0o700)
                sleep = root / "sleep"
                sleep.write_text("#!/bin/sh\nexit 0\n")
                sleep.chmod(0o700)
                env = {**os.environ, "RUNNER_TEMP": str(root), "PATH": str(root) + os.pathsep + os.environ["PATH"]}
                result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, expected_code, result.stderr)
                self.assertEqual(int((root / "calls").read_text()), expected_calls)


if __name__ == "__main__":
    unittest.main()
