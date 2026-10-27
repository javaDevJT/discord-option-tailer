"""Focused tests for resolving the stable Codex CLI version in release CI."""

from __future__ import annotations

import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/resolve_codex_cli.py"
WORKFLOW = ROOT / ".github/workflows/ci-release.yml"
SPEC = importlib.util.spec_from_file_location("resolve_codex_cli", SCRIPT)
RESOLVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RESOLVER)


class CodexCliReleaseTests(unittest.TestCase):
    def test_each_resolution_reads_latest_metadata_once(self) -> None:
        versions = iter(("0.164.0", "0.165.0"))
        calls: list[str] = []

        def fake_fetch() -> dict[str, str]:
            calls.append(RESOLVER.REGISTRY_URL)
            return {"name": RESOLVER.PACKAGE_NAME, "version": next(versions)}

        self.assertEqual(RESOLVER.resolve_latest(fake_fetch), "0.164.0")
        self.assertEqual(RESOLVER.resolve_latest(fake_fetch), "0.165.0")
        self.assertEqual(calls, [RESOLVER.REGISTRY_URL, RESOLVER.REGISTRY_URL])

    def test_registry_request_uses_only_the_fixed_official_url(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return RESOLVER.REGISTRY_URL

            def read(self, limit):
                self.limit = limit
                return json.dumps({"name": "@openai/codex", "version": "0.164.0"}).encode()

        class FakeOpener:
            def open(self, request, timeout):
                self.request = request
                self.timeout = timeout
                return FakeResponse()

        opener = FakeOpener()
        with patch.object(RESOLVER, "build_opener", return_value=opener):
            self.assertEqual(RESOLVER.validate_metadata(RESOLVER.fetch_latest_metadata()), "0.164.0")
        self.assertEqual(opener.request.full_url, RESOLVER.REGISTRY_URL)
        self.assertEqual(opener.request.get_header("Accept"), "application/json")

    def test_wrong_or_missing_package_metadata_fails(self) -> None:
        for metadata in (
            {"version": "0.164.0"},
            {"name": "another-package", "version": "0.164.0"},
            ["@openai/codex", "0.164.0"],
        ):
            with self.subTest(metadata=metadata), self.assertRaises(RESOLVER.ResolverError):
                RESOLVER.validate_metadata(metadata)

    def test_versions_must_be_strict_and_stable(self) -> None:
        for version in (
            "v0.164.0",
            "0.164",
            "0.164.0-beta.1",
            "01.164.0",
            "0.0164.0",
            "0.164.00",
            "0.164.0\nlatest",
            "",
            164,
        ):
            with self.subTest(version=version), self.assertRaises(RESOLVER.ResolverError):
                RESOLVER.validate_metadata({"name": "@openai/codex", "version": version})

    def test_version_output_contains_only_the_validated_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "github-output"
            RESOLVER.write_github_output("0.164.0", str(output))
            self.assertEqual(output.read_text(), "version=0.164.0\n")
            with self.assertRaises(RESOLVER.ResolverError):
                RESOLVER.write_github_output("0.164.0\ninjected=yes", str(output))
            self.assertEqual(output.read_text(), "version=0.164.0\n")

    def test_release_workflow_wires_concrete_version_to_the_build(self) -> None:
        workflow = WORKFLOW.read_text()
        resolver_steps = re.findall(
            r"(?m)^      - name: Resolve latest stable Codex CLI version\n"
            r"        id: codex_cli\n        run: python \.github/scripts/resolve_codex_cli\.py$",
            workflow,
        )
        self.assertEqual(len(resolver_steps), 1)
        build = re.search(
            r"(?ms)^      - name: Build local image archive\n(?P<step>.*?)(?=^      - name: Generate SBOM)",
            workflow,
        )
        self.assertIsNotNone(build)
        step = build.group("step")
        self.assertIn("CODEX_VERSION=${{ steps.codex_cli.outputs.version }}", step)
        self.assertIn("no-cache-filters: runtime,codex-runtime", step)
        self.assertEqual(workflow.count("python .github/scripts/resolve_codex_cli.py"), 1)


if __name__ == "__main__":
    unittest.main()
