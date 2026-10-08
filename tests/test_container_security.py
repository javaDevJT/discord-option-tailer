"""Synthetic tests for archive-bound container scanning and promotion."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/actions/container-security/container-security.sh"
PLATFORMS = "linux/amd64,linux/arm64"


def add_bytes(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = 0o644
    info.mtime = 0
    archive.addfile(info, io.BytesIO(content))


def write_oci_archive(path: Path, platforms: tuple[str, ...] = ("linux/amd64", "linux/arm64")) -> str:
    children = []
    for platform in platforms:
        os_name, architecture, *variant = platform.split("/")
        child = {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": "sha256:" + hashlib.sha256(platform.encode()).hexdigest(),
            "size": 1,
            "platform": {"os": os_name, "architecture": architecture},
        }
        if variant:
            child["platform"]["variant"] = variant[0]
        children.append(child)

    image_index = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": children,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    image_digest = "sha256:" + hashlib.sha256(image_index).hexdigest()
    outer_index = json.dumps(
        {
            "schemaVersion": 2,
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.index.v1+json",
                    "digest": image_digest,
                    "size": len(image_index),
                    "annotations": {"org.opencontainers.image.ref.name": "latest"},
                }
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    with tarfile.open(path, "w") as archive:
        add_bytes(archive, "oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
        add_bytes(archive, "index.json", outer_index)
        add_bytes(archive, f"blobs/sha256/{image_digest[7:]}", image_index)
    return image_digest


class ContainerSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.report = self.root / "report"
        self.archive = Path(f"{self.report}.oci.tar")
        self.image_digest = write_oci_archive(self.archive)
        self.oras_log = self.root / "oras.log"
        self.gh_output = self.root / "github-output"

        self.syft = self.root / "syft"
        self.syft.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$2\" >> \"$SYFT_SOURCE_LOG\"\n"
            "if [[ ${FAKE_SYFT_FAIL:-false} == true ]]; then exit 7; fi\n"
            "if [[ ${FAKE_SYFT_MUTATE:-false} == true ]]; then printf changed >> \"$SECURITY_ARCHIVE\"; fi\n"
            "for arg in \"$@\"; do\n"
            "  if [[ $arg == syft-json=* ]]; then printf '{\"artifacts\":[]}\\n' > \"${arg#syft-json=}\"; fi\n"
            "done\n"
        )
        self.grype = self.root / "grype"
        self.grype.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$*\" >> \"$GRYPE_LOG\"\n"
            "for ((i=1; i<=$#; i++)); do\n"
            "  if [[ ${!i} == --file ]]; then j=$((i+1)); printf '{\"matches\":[]}\\n' > \"${!j}\"; fi\n"
            "done\n"
            "if [[ ${FAKE_GRYPE_FAIL:-false} == true ]]; then exit 2; fi\n"
        )
        self.oras = self.root / "oras"
        self.oras.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf '%s\\n' \"$*\" >> \"$ORAS_LOG\"\n"
            "if [[ $1 == manifest && $2 == fetch ]]; then printf '{\"digest\":\"%s\"}\\n' \"$ORAS_DIGEST\"; fi\n"
        )
        for tool in (self.syft, self.grype, self.oras):
            tool.chmod(0o755)

        self.grype_log = self.root / "grype.log"
        self.syft_source_log = self.root / "syft-source.log"
        self.env = os.environ.copy()
        self.env.update(
            {
                "SECURITY_REPORT_DIRECTORY": str(self.report),
                "SECURITY_PUBLISH": "true",
                "SECURITY_RELEASE_TAGS": "ghcr.io/example/relay:1.2.3\nghcr.io/example/relay:latest",
                "SECURITY_ARCHIVE": str(self.archive),
                "SECURITY_PLATFORMS": PLATFORMS,
                "SYFT_CMD": str(self.syft),
                "GRYPE_CMD": str(self.grype),
                "ORAS_CMD": str(self.oras),
                "ORAS_LOG": str(self.oras_log),
                "ORAS_DIGEST": self.image_digest,
                "GRYPE_LOG": str(self.grype_log),
                "SYFT_SOURCE_LOG": str(self.syft_source_log),
                "GITHUB_OUTPUT": str(self.gh_output),
            }
        )
        self.run_action("prepare")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_action(self, action: str, *, success: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["bash", str(SCRIPT), action],
            cwd=ROOT,
            env=env or self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        if success and result.returncode != 0:
            self.fail(f"{action} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
        if not success and result.returncode == 0:
            self.fail(f"{action} unexpectedly succeeded:\n{result.stdout}")
        return result

    def scan(self, *, success: bool = True, **overrides: str) -> subprocess.CompletedProcess[str]:
        env = self.env.copy()
        env.update(overrides)
        return self.run_action("scan", success=success, env=env)

    def publish(self, *, success: bool = True) -> subprocess.CompletedProcess[str]:
        return self.run_action("publish", success=success)

    def test_prepare_exports_only_to_named_local_oci_archive(self) -> None:
        outputs = dict(line.split("=", 1) for line in self.gh_output.read_text().splitlines())
        self.assertEqual(outputs["archive"], str(self.archive))
        self.assertEqual(
            outputs["exporter"],
            f"type=oci,dest={self.archive},name=security-scan",
        )
        self.assertNotIn("push", outputs["exporter"])

    def test_prepare_accepts_metadata_whitespace_and_rejects_invalid_tags(self) -> None:
        env = self.env.copy()
        env["SECURITY_RELEASE_TAGS"] = "\n " + self.env["SECURITY_RELEASE_TAGS"] + "\n\n"
        self.run_action("prepare", env=env)
        self.assertEqual((self.report / "release-tags.txt").read_text().splitlines(), self.env["SECURITY_RELEASE_TAGS"].splitlines())
        for tags in ("\n \n", "ghcr.io/example/relay:latest\nghcr.io/example/relay:latest", "not-a-registry-tag"):
            env["SECURITY_RELEASE_TAGS"] = tags
            self.run_action("prepare", success=False, env=env)

    def test_syft_resolves_archive_path_without_an_oci_reference_suffix(self) -> None:
        self.scan()
        sources = self.syft_source_log.read_text().splitlines()
        self.assertEqual(len(sources), 2)
        self.assertTrue(all(source.startswith("oci-archive:") for source in sources))
        self.assertTrue(all(source.endswith(".oci.tar") for source in sources))

    def test_syft_failure_leaves_no_publishable_pass_receipt(self) -> None:
        self.scan(success=False, FAKE_SYFT_FAIL="true")
        self.assertFalse((self.report / "scanned-archive.sha256").exists())
        self.assertIn("FAIL linux/amd64", (self.report / "gate.txt").read_text())
        self.publish(success=False)
        self.assertFalse(self.oras_log.exists())

    def test_grype_high_gate_failure_does_not_promote(self) -> None:
        env = self.env.copy()
        env["FAKE_GRYPE_FAIL"] = "true"
        self.run_action("scan", success=False, env=env)
        self.assertIn("--fail-on high", self.grype_log.read_text())
        self.assertFalse((self.report / "scanned-archive.sha256").exists())
        self.publish(success=False)
        self.assertFalse(self.oras_log.exists())

    def test_archive_modification_during_scan_fails_closed(self) -> None:
        self.scan(success=False, FAKE_SYFT_MUTATE="true")
        self.assertIn("FAIL archive-integrity", (self.report / "gate.txt").read_text())
        self.publish(success=False)
        self.assertFalse(self.oras_log.exists())

    def test_publish_rejects_missing_extra_duplicate_empty_and_failed_pass_rows(self) -> None:
        self.scan()
        invalid_gates = (
            "PASS linux/amd64\n",
            "PASS linux/amd64\nPASS linux/arm64\nPASS linux/arm/v7\n",
            "PASS linux/amd64\nPASS linux/amd64\n",
            "PASS linux/amd64\n\nPASS linux/arm64\n",
            "PASS linux/amd64\nFAIL linux/arm64\n",
        )
        for gate in invalid_gates:
            with self.subTest(gate=gate):
                self.oras_log.unlink(missing_ok=True)
                (self.report / "gate.txt").write_text(gate)
                self.publish(success=False)
                self.assertFalse(self.oras_log.exists(), "ORAS ran before exact PASS coverage was proven")

    def test_modified_archive_after_scan_is_not_published(self) -> None:
        self.scan()
        with self.archive.open("ab") as archive:
            archive.write(b"changed")
        self.publish(success=False)
        self.assertFalse(self.oras_log.exists())

    def test_downloaded_candidate_preserves_scan_binding(self) -> None:
        self.scan()
        for modified in (False, True):
            with self.subTest(modified=modified):
                destination = self.root / f"download-{modified}"
                destination.mkdir()
                report = destination / "build-1"
                archive = destination / "build-1.oci.tar"
                shutil.copytree(self.report, report)
                shutil.copyfile(self.archive, archive)
                if modified:
                    with archive.open("ab") as output:
                        output.write(b"changed in transit")
                self.oras_log.unlink(missing_ok=True)
                env = {**self.env, "SECURITY_REPORT_DIRECTORY": str(report), "SECURITY_ARCHIVE": str(archive)}
                result = self.run_action("publish", success=not modified, env=env)
                if modified:
                    self.assertIn("OCI archive changed after security scanning", result.stderr)
                    self.assertFalse(self.oras_log.exists())
                else:
                    self.assertEqual(len((report / "published-digests.txt").read_text().splitlines()), 2)

    def test_workflow_waits_for_completed_build_before_publication(self) -> None:
        workflow = (ROOT / ".github/workflows/ci-release.yml").read_text()
        build, publish = workflow.split("  build:\n", 1)[1].split("  publish:\n", 1)
        publication_job, publication_steps = publish.split("    steps:\n", 1)
        self.assertIn("    needs: test\n", build)
        self.assertIn("    runs-on: truenas-discord-option-tailer-storage-11g\n", build)
        self.assertIn("      packages: read\n", build)
        self.assertNotIn("packages: write", build)
        self.assertIn("    needs: [test, build]\n", publication_job)
        self.assertIn("    runs-on: ubuntu-latest\n", publication_job)
        self.assertNotRegex(publication_job, r"(?m)^    (if|continue-on-error):")
        self.assertNotIn("continue-on-error:", workflow)
        self.assertNotIn("ci-storage-efficiency", workflow)
        self.assertNotIn("container-security.sh publish", build)
        self.assertIn("          push: false\n", build)
        names = re.findall(r"^      - name: (.+)$", build, re.MULTILINE)
        self.assertLess(names.index("Build local image archive"), names.index("Generate SBOM and enforce High/Critical vulnerability gate"))
        self.assertLess(names.index("Generate SBOM and enforce High/Critical vulnerability gate"), names.index("Retain scanned candidate for gated publication"))
        self.assertEqual(build.count("${{ steps.container_security_1.outputs.archive }}"), 2)
        self.assertIn("candidate_artifact_id: ${{ steps.candidate.outputs.artifact-id }}", build)
        self.assertIn("artifact-ids: ${{ needs.build.outputs.candidate_artifact_id }}", publication_steps)
        self.assertIn("digest-mismatch: error", publication_steps)
        self.assertIn("SECURITY_ARCHIVE: ${{ runner.temp }}/container-security/build-1.oci.tar", publication_steps)
        publication = publication_steps.split("      - name: Publish scanned archive only after all gates pass\n", 1)[1].split("      - name:", 1)[0]
        self.assertNotIn("if:", publication)
        self.assertIn("container-security.sh publish", publication)

    def test_mismatched_remote_digest_stops_remaining_tags(self) -> None:
        self.scan()
        env = self.env.copy()
        env["ORAS_DIGEST"] = "sha256:" + "f" * 64
        result = self.run_action("publish", success=False, env=env)
        self.assertIn("Published image digest differs", result.stderr)
        copies = [line for line in self.oras_log.read_text().splitlines() if line.startswith("cp ")]
        self.assertEqual(len(copies), 1)
        self.assertEqual((self.report / "published-digests.txt").read_text(), "")

    def test_exact_pass_coverage_promotes_archive_digest_to_each_release_tag(self) -> None:
        self.scan()
        self.publish()
        calls = self.oras_log.read_text().splitlines()
        copies = [call for call in calls if call.startswith("cp ")]
        fetches = [call for call in calls if call.startswith("manifest fetch --descriptor ")]
        self.assertEqual(len(copies), 2)
        self.assertTrue(all("--from-oci-layout" in call for call in copies))
        self.assertEqual(len(fetches), 2)
        self.assertEqual(
            (self.report / "published-digests.txt").read_text().splitlines(),
            [
                f"ghcr.io/example/relay:1.2.3 {self.image_digest}",
                f"ghcr.io/example/relay:latest {self.image_digest}",
            ],
        )


if __name__ == "__main__":
    unittest.main()
