"""Start only the private Collector, failing closed on missing/unreadable secrets."""
import os
from pathlib import Path
import signal
import stat
import sys

from .telemetry import enabled


def main():
    if not enabled():
        # Standby avoids changing the rest of the app's lifecycle when not configured.
        while True:
            signal.pause()
    path = Path(os.environ.get("OPENWOOF_INGEST_KEY_FILE", ""))
    try:
        info = path.lstat()
        if (str(path) != "/run/secrets/openwoof-ingest-key" or not stat.S_ISREG(info.st_mode)
                or info.st_uid != 0 or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}
                or not 0 < info.st_size <= 4096 or not os.access(path, os.R_OK)):
            raise ValueError
    except (OSError, ValueError):
        print("OpenWoof Collector disabled: ingest key file must be a nonempty, readable, owner-only regular file at the configured mount.", file=sys.stderr)
        raise SystemExit(2)
    Path("/data/telemetry/collector-checkpoints").mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in tuple(os.environ):
        if name.startswith("OTEL_EXPORTER_OTLP_"):
            os.environ.pop(name)
    os.execv("/usr/local/bin/otelcol-contrib", ["otelcol-contrib", "--config=/opt/relay-docker/otelcol.yaml"])


if __name__ == "__main__":
    main()
