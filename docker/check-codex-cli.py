"""Offline build contract for automatic Codex CLI updates; synthetic data only."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def check(executable):
    with tempfile.TemporaryDirectory(prefix="relay-codex-build-check-") as directory:
        root = Path(directory)
        home, workspace, persistent = (root / name for name in ("home", "workspace", "persistent"))
        for path in (home, workspace, persistent):
            path.mkdir(mode=0o700)
        credential = persistent / "auth.json"
        link = home / "auth.json"
        link.symlink_to(credential)
        env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
               "HOME": str(root), "CODEX_HOME": str(home)}

        def run(args, input=None):
            result = subprocess.run([str(Path(executable).resolve()), *args], cwd=workspace, env=env,
                                    input=input, text=True, capture_output=True, timeout=20)
            if result.returncode:
                raise RuntimeError("Codex CLI failed its offline compatibility contract")

        run(["exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--strict-config",
             "--skip-git-repo-check", "--sandbox", "read-only", "--json", "--help"])
        # Parse the production device-login flags without starting sign-in.
        run(["login", "--device-auth", "-c", 'cli_auth_credentials_store="file"', "--help"])
        # This command only stores a synthetic marker. Docker runs this entire
        # check with networking disabled, so it cannot authenticate or bill.
        marker = "sk-build-validation-not-a-real-credential"
        for existing in (False, True):
            if existing:
                credential.write_text(json.dumps({"OPENAI_API_KEY": "previous-synthetic-marker"}))
            run(["login", "--with-api-key", "-c", 'cli_auth_credentials_store="file"'], marker + "\n")
            if not link.is_symlink() or not credential.is_file():
                raise RuntimeError("Codex CLI replaced the persistent credential link")
            if json.loads(credential.read_text()).get("OPENAI_API_KEY") != marker:
                raise RuntimeError("Codex CLI did not preserve file-backed credential writes")
        if (credential.stat().st_mode & 0o077) != 0:
            raise RuntimeError("Codex CLI created an insecure credential file")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", default="/usr/local/bin/codex")
    check(parser.parse_args().executable)
    print("Codex offline isolation and credential-storage contracts passed")
