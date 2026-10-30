from pathlib import Path
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from relay.resources import ResourceDiagnostics, collect_resources, MAX_FDS, MAX_PROCESSES


class ResourceSnapshotTests(unittest.TestCase):
    def test_reports_actual_resource_values_without_argv_or_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            proc, cgroup = root / "proc", root / "cgroup"
            process = proc / "42"
            (process / "fd").mkdir(parents=True)
            cgroup.mkdir()
            (process / "status").write_text("Name:\tpython\nPPid:\t1\nVmRSS:\t123 kB\nThreads:\t7\n")
            (process / "cmdline").write_text("secret-in-arguments")
            (process / "environ").write_text("secret-in-environment")
            (process / "fd/0").touch()
            (process / "fd/1").touch()
            for name, value in {"memory.current": "65536", "memory.peak": "131072", "memory.max": "max",
                                "pids.current": "9", "pids.max": "128", "memory.events": "oom 3\noom_kill 2\nhigh 7\n"}.items():
                (cgroup / name).write_text(value)
            value = collect_resources(root, proc_root=proc, cgroup_root=cgroup)
            self.assertTrue(value["available"])
            self.assertEqual(value["container"]["memory_current_bytes"], 65536)
            self.assertIsNone(value["container"]["memory_limit_bytes"])
            self.assertTrue(value["container"]["memory_limit_unlimited"])
            self.assertEqual(value["container"]["memory_events"]["oom_kill"], 2)
            self.assertEqual(value["processes"][0]["resident_bytes"], 123 * 1024)
            self.assertEqual(value["processes"][0]["open_fds"], 2)
            self.assertEqual(value["processes"][0]["threads"], 7)
            self.assertNotIn("secret-in-", json.dumps(value))

    def test_missing_or_malformed_data_stays_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "memory.current").write_text("not a number")
            (root / "memory.max").write_text("-1")
            (root / "memory.events").write_text("oom_kill invalid\n")
            value = collect_resources(root / "missing", proc_root=root / "missing", cgroup_root=root)
            self.assertFalse(value["available"])
            self.assertIsNone(value["container"]["memory_current_bytes"])
            self.assertIsNone(value["container"]["memory_limit_bytes"])
            self.assertIsNone(value["container"]["memory_limit_unlimited"])
            self.assertIsNone(value["container"]["memory_events"]["oom_kill"])
            self.assertIsNone(value["data_filesystem"])

    def test_namespace_and_descriptor_scans_have_explicit_caps(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for pid in range(MAX_PROCESSES + 1):
                directory = root / str(pid)
                directory.mkdir()
                (directory / "status").write_text("Name:\tpython\n")
            value = collect_resources(root, proc_root=root, cgroup_root=root / "missing")
            self.assertEqual(len(value["processes"]), MAX_PROCESSES)
            self.assertTrue(value["processes_truncated"])

    def test_descriptor_scan_reports_a_lower_bound_when_truncated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            process = root / "12"
            (process / "fd").mkdir(parents=True)
            (process / "status").write_text("Name:\tpython\n")
            for number in range(MAX_FDS + 1):
                (process / "fd" / str(number)).touch()
            value = collect_resources(root, proc_root=root, cgroup_root=root / "missing")
            self.assertEqual(value["processes"][0]["open_fds"], MAX_FDS)
            self.assertTrue(value["processes"][0]["open_fds_truncated"])


class CachedResourceTests(unittest.TestCase):
    def test_slow_collection_never_blocks_requests_or_starts_extra_collectors(self):
        started, release = threading.Event(), threading.Event()
        calls = []

        def slow_collect(_directory):
            calls.append(True)
            started.set()
            release.wait(2)
            return {"available": True, "container": {"memory_current_bytes": 123}}

        diagnostics = ResourceDiagnostics("unused", collect=slow_collect)
        begin = time.monotonic()
        first = diagnostics.snapshot()
        self.assertLess(time.monotonic() - begin, 0.2)
        self.assertTrue(first["collecting"])
        self.assertTrue(started.wait(1))
        try:
            for _ in range(20):
                self.assertTrue(diagnostics.snapshot()["collecting"])
            self.assertEqual(len(calls), 1)
        finally:
            release.set()
        deadline = time.monotonic() + 2
        while diagnostics.snapshot()["collecting"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(diagnostics.snapshot()["container"]["memory_current_bytes"], 123)
        self.assertFalse(diagnostics.snapshot()["stale"])

    def test_thread_exhaustion_returns_diagnostic_error_without_crashing_status(self):
        diagnostics = ResourceDiagnostics("unused")
        with patch("threading.Thread.start", side_effect=RuntimeError("sensitive detail")) as start:
            result = diagnostics.snapshot()
            for _ in range(20):
                diagnostics.snapshot()
            self.assertEqual(start.call_count, 1)
        self.assertFalse(result["available"])
        self.assertFalse(result["collecting"])
        self.assertEqual(result["collection_error"], "RuntimeError")
        self.assertNotIn("sensitive detail", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
