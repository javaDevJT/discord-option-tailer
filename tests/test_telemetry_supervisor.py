"""Exercise the real Supervisor event protocol and retain original child output."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest


class NativeSupervisorOutputTests(unittest.TestCase):
    def test_native_process_events_copy_complete_original_output_once(self):
        import supervisor  # noqa: F401 - pinned test extra, required by CI
        root = Path(__file__).resolve().parents[1]
        body = json.dumps({"state": "held", "reason": "retain ‘detail’ and context"}, indent=2, ensure_ascii=False)
        diagnostic = "WARNING:fixture.source:original subprocess diagnostic\n"
        actor = f"import sys;print({body!r},flush=True);sys.stderr.write({diagnostic!r});sys.stderr.flush()"
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            listener = f"from relay.telemetry_logs import main;main({directory!r})"
            configuration = f"""[supervisord]
nodaemon=true
logfile={temp / 'supervisord.log'}
pidfile={temp / 'supervisord.pid'}
childlogdir={directory}

[eventlistener:telemetry-output]
command={shlex.join([sys.executable, '-c', listener])}
directory={root}
events=PROCESS_LOG_STDOUT,PROCESS_LOG_STDERR,TICK_5
priority=6
buffer_size=64
startsecs=0
stderr_logfile={temp / 'listener.stderr'}

[program:service]
command={shlex.join([sys.executable, '-c', actor])}
priority=10
startsecs=0
autorestart=false
stdout_events_enabled=true
stdout_logfile={temp / 'original.stdout'}
stdout_logfile_maxbytes=0
redirect_stderr=true
"""
            config = temp / "supervisor.conf"; config.write_text(configuration)
            with (temp / "parent.stdout").open("wb") as output:
                process = subprocess.Popen([sys.executable, "-m", "supervisor.supervisord", "-c", str(config)],
                    env=dict(os.environ, RELAY_OTEL_ENABLED="true"), stdout=output, stderr=subprocess.STDOUT, cwd=root)
                try:
                    deadline = time.monotonic() + 10
                    copied = temp / "process-output.jsonl"
                    while time.monotonic() < deadline:
                        if copied.exists() and len(copied.read_text().splitlines()) >= 2:
                            break
                        time.sleep(0.05)
                    self.assertTrue(copied.exists(), (temp / "listener.stderr").read_text() if (temp / "listener.stderr").exists() else "listener did not produce a copy")
                    records = [json.loads(line) for line in copied.read_text().splitlines()]
                    self.assertEqual((temp / "original.stdout").read_text(), body + "\n" + diagnostic)
                    self.assertEqual([record["body"] for record in records], [body, diagnostic.rstrip("\n")])
                    self.assertEqual(records[1]["severity"], "WARNING")
                    self.assertEqual(records[1]["attributes"]["log.logger.name"], "fixture.source")
                finally:
                    process.terminate()
                    try: process.wait(timeout=5)
                    except subprocess.TimeoutExpired: process.kill(); process.wait()


if __name__ == "__main__": unittest.main()
