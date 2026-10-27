"""Offline Codex auth isolation and diagnostics; synthetic credentials only."""

import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from relay.codex_auth import isolated_auth_home
from relay.interpreter import CodexInterpreter, InterpretationError, _diagnostic_code
from relay.setup import SetupManager, _AuthJob

ROOT = Path(__file__).resolve().parents[1]


class CodexAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / "codex"
        self.home.mkdir()
        config = json.loads((ROOT / "config.example.json").read_text())
        config.update(mode="shadow", database="state/test.sqlite3", kill_switch="state/STOP")
        config["discord"]["transport"] = "browser"
        self.config_path = self.base / "config.json"
        self.config_path.write_text(json.dumps(config))
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        self.env.start()
        self.manager = SetupManager(self.config_path)

    def tearDown(self):
        self.manager.close()
        self.env.stop()
        self.temp.cleanup()

    def test_isolated_home_writes_through_and_never_removes_persistent_auth(self):
        (self.home / "config.toml").write_text("broken inference configuration")
        for existing in (False, True):
            credential = self.home / "auth.json"
            if existing:
                credential.write_text("previous synthetic marker")
            with isolated_auth_home(self.home) as home:
                self.assertFalse((home / "config.toml").exists())
                self.assertTrue((home / "auth.json").is_symlink())
                (home / "auth.json").write_text("replacement synthetic marker")
                self.assertEqual(credential.read_text(), "replacement synthetic marker")
            self.assertFalse(home.exists())
            self.assertEqual(credential.read_text(), "replacement synthetic marker")

    def test_login_status_ignores_bad_user_config_without_model_call(self):
        (self.home / "auth.json").write_text("synthetic marker")
        (self.home / "config.toml").write_text("this is not TOML = [")
        interpreter = CodexInterpreter({"llm": {"executable": sys.executable}})
        homes = []

        def status(args, env, cwd, input_bytes=None):
            home = Path(env["CODEX_HOME"])
            homes.append(home)
            self.assertNotEqual(home, self.home)
            self.assertFalse((home / "config.toml").exists())
            self.assertTrue((home / "auth.json").is_symlink())
            self.assertEqual(args[1:3], ["login", "status"])
            self.assertIn('cli_auth_credentials_store="file"', args)
            self.assertIsNone(input_bytes)
            return subprocess.CompletedProcess(args, 0, b"Logged in using ChatGPT", b"")

        with patch.object(interpreter, "_run_process", side_effect=status) as run:
            result = asyncio.run(interpreter.subscription_status())
        self.assertTrue(result["authenticated"])
        self.assertEqual(run.call_count, 1)
        self.assertFalse(homes[0].exists())
        self.assertEqual((self.home / "auth.json").read_text(), "synthetic marker")
        with self.assertRaises(InterpretationError):
            interpreter._default_model()

    def test_device_login_promotes_verified_auth_and_invalidates_negative_status_cache(self):
        for existing in (False, True):
            with self.subTest(existing_auth=existing):
                self._device_login_replaces_persistent_auth(existing)

    def test_failed_cancelled_or_unverified_device_login_preserves_previous_auth(self):
        for existing in (False, True):
            for outcome in ("failed", "cancelled", "timeout", "unverified", "promotion_failed"):
                with self.subTest(existing_auth=existing, outcome=outcome):
                    self._device_login_replaces_persistent_auth(existing, outcome)

    def _device_login_replaces_persistent_auth(self, existing, outcome="success"):
        credential = self.home / "auth.json"
        credential.unlink(missing_ok=True)
        if existing:
            credential.write_text("previous synthetic marker")
        (self.home / "config.toml").write_text("invalid inference configuration = [")
        captures = {}
        job = _AuthJob("codex")

        class Process:
            stdout = io.StringIO("Open https://auth.openai.com/codex/device\nEnter code ABCD-1234\n")

            def poll(self): return 0
            def wait(self, timeout=None):
                if existing:
                    assert credential.read_text() == "previous synthetic marker"
                else:
                    assert not credential.exists()
                # Official device login removes the old store before saving
                # the new one, unlike refresh/API-key write-through paths.
                (captures["home"] / "auth.json").unlink(missing_ok=True)
                (captures["home"] / "auth.json").write_text("new synthetic marker")
                if outcome == "cancelled":
                    job.cancelled.set()
                job.timed_out = outcome == "timeout"
                return 1 if outcome == "failed" else 0

        def start(args, **kwargs):
            home = Path(kwargs["env"]["CODEX_HOME"])
            captures["home"] = home
            self.assertEqual(Path(kwargs["cwd"]), home)
            self.assertNotEqual(home, self.home.resolve())
            self.assertEqual(home.parent, self.home.resolve())
            self.assertFalse((home / "auth.json").exists())
            self.assertFalse((home / "auth.json").is_symlink())
            self.assertFalse((home / "config.toml").exists())
            self.assertEqual(args[1:3], ["login", "--device-auth"])
            self.assertIn('cli_auth_credentials_store="file"', args)
            return Process()

        self.manager._codex_check = ((1, 1), float("inf"), False)
        with patch.object(self.manager, "_codex_executable", return_value=sys.executable), \
                patch("relay.setup.subprocess.Popen", side_effect=start), \
                patch("relay.setup.CodexInterpreter") as interpreter:
            interpreter.return_value.subscription_status = AsyncMock(return_value={
                "authenticated": outcome != "unverified", "isolated_execution_available": True})
            if outcome == "promotion_failed":
                with patch("relay.setup.os.replace", side_effect=PermissionError("synthetic failure")):
                    self.manager._run_codex_job(job)
            else:
                self.manager._run_codex_job(job)
            if outcome in {"success", "unverified", "promotion_failed"}:
                self.assertNotIn("model", interpreter.call_args.args[0]["llm"])
                self.assertEqual(interpreter.call_args.kwargs["source_home"], captures["home"])
            else:
                interpreter.assert_not_called()
        if outcome == "success":
            self.assertEqual(self.manager._auth_state["codex"]["state"], "connected")
            self.assertEqual(credential.read_text(), "new synthetic marker")
            self.assertEqual(credential.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.manager._codex_check, (None, 0.0, False))
        else:
            self.assertEqual(self.manager._auth_state["codex"]["state"],
                             "cancelled" if outcome == "cancelled" else "failed")
            if existing:
                self.assertEqual(credential.read_text(), "previous synthetic marker")
            else:
                self.assertFalse(credential.exists())
        self.assertFalse(captures["home"].exists())

    def test_specific_login_failure_survives_generic_epilogue_and_restart(self):
        class Process:
            stdout = io.StringIO(
                "HTTP 403 Forbidden: Device code authentication is not enabled secret=do-not-retain\n"
                "Device login failed\n")
            def poll(self): return 1
            def wait(self, timeout=None): return 1

        job = _AuthJob("codex")
        with patch.object(self.manager, "_codex_executable", return_value=sys.executable), \
                patch("relay.setup.subprocess.Popen", return_value=Process()), \
                self.assertLogs("relay.setup", level="WARNING") as logs:
            self.manager._run_codex_job(job)
        self.assertEqual(job.failure_code, "device_auth_disabled")
        failure = self.manager.status()["codex"]["failure"]
        self.assertEqual(failure["http_status"], 403)
        self.assertEqual(failure["exit_code"], 1)
        self.assertEqual(failure["attempt_id"], job.attempt_id)
        path = self.base / "state/codex-auth-diagnostic.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("do-not-retain", path.read_text() + str(logs.output))
        restarted = SetupManager(self.config_path)
        try:
            result = restarted.status()["codex"]["last_failure"]
            self.assertEqual(result["failure"], failure)
            self.assertIn("ChatGPT security", result["detail"])
            value = json.loads(path.read_text()) | {"secret": "do-not-retain", "exit_code": "do-not-retain"}
            path.write_text(json.dumps(value))
            self.assertNotIn("do-not-retain", json.dumps(restarted.status()["codex"]["last_failure"]))
        finally:
            restarted.close()

    def test_malformed_saved_diagnostic_does_not_break_status(self):
        path = self.base / "state/codex-auth-diagnostic.json"
        path.parent.mkdir()
        for value in ({"phase": [], "code": "cli_failed"},
                      {"phase": "device_login", "code": {}},
                      {"phase": "unknown", "code": "cli_failed"}):
            with self.subTest(value=value):
                path.write_text(json.dumps(value))
                self.assertIsNone(self.manager._read_codex_failure())

    def test_model_and_auth_errors_remain_distinct(self):
        self.assertEqual(_diagnostic_code("The 'new-model' model is not supported with this ChatGPT account"),
                         "model_unavailable")
        self.assertEqual(_diagnostic_code("ChatGPT authentication failed"), "auth_required")
        for effort in ("max", "ultra", "reasoning-vNext_2026"):
            interpreter = CodexInterpreter({"llm": {"executable": sys.executable,
                                                      "model": "gpt-6.1-sol", "reasoning_effort": effort}})
            self.assertEqual(interpreter.reasoning_effort, effort)
