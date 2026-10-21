"""Bootstrap optional telemetry before the selected application module imports."""
import os
import runpy
import signal
import sys

from .telemetry import initialize, shutdown


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in {"dashboard", "service", "notifications"}:
        raise SystemExit("expected dashboard, service, or notifications")
    program = sys.argv.pop(1)
    runtime = initialize(program)
    if runtime is not None:
        previous = signal.getsignal(signal.SIGTERM)

        def terminate(signum, frame):
            shutdown()
            if callable(previous):
                previous(signum, frame)
            else:
                signal.signal(signum, previous)
                if previous == signal.SIG_DFL:
                    os.kill(os.getpid(), signum)

        signal.signal(signal.SIGTERM, terminate)
    try:
        runpy.run_module("relay." + program, run_name="__main__", alter_sys=True)
    finally:
        shutdown()


if __name__ == "__main__":
    main()
