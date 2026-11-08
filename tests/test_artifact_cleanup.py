"""Synthetic tests for preserving security reports on transient download errors."""

from __future__ import annotations

import importlib.util
import io
import json
import unittest
import zipfile
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/cleanup-actions-artifacts.py"
SPEC = importlib.util.spec_from_file_location("cleanup_actions_artifacts", SCRIPT)
JANITOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(JANITOR)


def report_archive(repository: str, revision: str, *, valid: bool = True) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if valid:
            report = {
                "schemaVersion": 1,
                "reports": [{"name": "grype.json", "tool": "grype", "highCriticalCount": 1}],
                "findings": [{
                    "tool": "grype",
                    "report": "grype.json",
                    "severity": "HIGH",
                    "id": "CVE-2026-12345",
                }],
                "repository": repository,
                "revision": revision,
            }
        else:
            report = {"unexpected": True}
        archive.writestr("security-high-critical.json", json.dumps(report))
    return output.getvalue()


class FakeApi:
    def __init__(self, run: dict, artifact: dict, archive: bytes, *, download_error=None) -> None:
        self.run = run
        self.artifact = artifact
        self.archive = archive
        self.download_error = download_error
        self.deleted: list[int] = []

    def get_workflow_run(self, _repository: str, _run_id: int) -> dict:
        return self.run

    def list_run_artifacts(self, _repository: str, _run_id: int) -> list[dict]:
        return [self.artifact]

    def download_artifact(self, _repository: str, _artifact_id: int) -> bytes:
        if self.download_error is not None:
            raise self.download_error
        return self.archive

    def delete_artifact(self, _repository: str, artifact_id: int) -> None:
        self.deleted.append(artifact_id)


class ArtifactCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = "javaDevJT/discord-option-tailer"
        self.run_id = 41192847314
        self.repository_id = 123
        self.revision = "a" * 40
        self.now = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
        self.run_created = self.now - timedelta(minutes=30)
        self.artifact_created = self.now - timedelta(minutes=1)
        self.archive = report_archive(self.repository, self.revision)
        self.run = {
            "id": self.run_id,
            "status": "completed",
            "repository": {"full_name": self.repository, "id": self.repository_id},
            "head_repository": {"id": self.repository_id},
            "head_sha": self.revision,
            "event": "schedule",
            "created_at": self.run_created.isoformat(),
        }
        self.artifact = {
            "id": 13040997553,
            "name": "security-high-critical-build-41192847314-1",
            "expired": False,
            "created_at": self.artifact_created.isoformat(),
            "expires_at": (self.artifact_created + timedelta(hours=24)).isoformat(),
            "size_in_bytes": len(self.archive),
            "workflow_run": {
                "id": self.run_id,
                "head_sha": self.revision,
                "repository_id": self.repository_id,
                "head_repository_id": self.repository_id,
                "repository": {"full_name": self.repository},
            },
        }

    def run_cleanup(self, api: FakeApi) -> tuple[bool, list[str]]:
        messages: list[str] = []
        janitor = JANITOR.Janitor(api, self.repository, now=self.now, log=messages.append)
        return janitor.cleanup(self.run_id), messages

    def test_security_report_download_failure_is_preserved_and_fails_cleanup(self) -> None:
        api = FakeApi(
            self.run,
            self.artifact,
            self.archive,
            download_error=JANITOR.JanitorError("temporary download failure"),
        )
        errors = io.StringIO()

        with (
            patch.object(JANITOR, "GitHubApi", return_value=api),
            patch.object(JANITOR, "datetime", wraps=datetime) as clock,
            redirect_stderr(errors),
        ):
            clock.now.return_value = self.now
            exit_code = JANITOR.main(
                ["--run-id", str(self.run_id)],
                environ={"GITHUB_REPOSITORY": self.repository, "GH_TOKEN": "synthetic-token"},
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(api.deleted, [])
        self.assertIn("reason=download_failed", errors.getvalue())

    def test_valid_security_report_is_kept(self) -> None:
        api = FakeApi(self.run, self.artifact, self.archive)

        succeeded, messages = self.run_cleanup(api)

        self.assertTrue(succeeded)
        self.assertEqual(api.deleted, [])
        self.assertTrue(any("reason=validated" in message for message in messages))

    def test_positively_invalid_security_report_is_deleted(self) -> None:
        invalid_archive = report_archive(self.repository, self.revision, valid=False)
        artifact = {**self.artifact, "size_in_bytes": len(invalid_archive)}
        api = FakeApi(self.run, artifact, invalid_archive)

        succeeded, messages = self.run_cleanup(api)

        self.assertTrue(succeeded)
        self.assertEqual(api.deleted, [self.artifact["id"]])
        self.assertTrue(any("reason=content_invalid" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
