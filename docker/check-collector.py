#!/usr/bin/env python3
"""Check the packaged Collector using synthetic loopback-only telemetry."""

import argparse
from contextlib import ExitStack
import base64
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

TOKEN = "fixture"
PATHS = ("/v1/traces", "/v1/metrics", "/v1/logs")


class CheckFailure(RuntimeError):
    pass


def free_port(used):
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in used:
            used.add(port)
            return port


def rewrite(config, replacements):
    for old, new, label in replacements:
        if config.count(old) != 1:
            raise CheckFailure(f"production config no longer has one expected {label}")
        config = config.replace(old, new, 1)
    return config


def attr_strings(attributes):
    return {
        item.key: item.value.string_value
        for item in attributes
        if item.value.WhichOneof("value") == "string_value"
    }


def wait_for_health(process, port, opener):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise CheckFailure("Collector exited before its health endpoint became ready")
        try:
            with opener.open(f"http://127.0.0.1:{port}/", timeout=0.5):
                return
        except (OSError, URLError):
            time.sleep(0.1)
    raise CheckFailure("Collector did not become ready within 20 seconds")


def send_otlp(port, path, message, opener):
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        data=message.SerializeToString(),
        headers={"Content-Type": "application/x-protobuf"},
        method="POST",
    )
    with opener.open(request, timeout=3) as response:
        if response.status != 200:
            raise CheckFailure(f"OTLP receiver rejected {path}")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--executable", default="/usr/local/bin/otelcol-contrib")
    cli.add_argument("--config", default="/opt/relay-docker/otelcol.yaml")
    args = cli.parse_args()
    executable = Path(args.executable)
    if not executable.is_file():
        found = shutil.which(args.executable)
        if not found:
            raise CheckFailure("Collector executable was not found")
        executable = Path(found)
    config_path = Path(args.config)
    if not config_path.is_file():
        raise CheckFailure("production Collector config was not found")

    try:
        from google.protobuf.json_format import ParseDict
        from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
        from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    except ImportError:
        raise CheckFailure("runtime protobuf and OTLP proto modules are required") from None

    types = {
        "/v1/traces": ExportTraceServiceRequest,
        "/v1/metrics": ExportMetricsServiceRequest,
        "/v1/logs": ExportLogsServiceRequest,
    }
    received = {path: [] for path in PATHS}
    errors = []
    changed = threading.Condition()

    def record_error(name):
        with changed:
            errors.append(name)
            changed.notify_all()

    class Sink(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4 * 1024 * 1024:
                    raise ValueError
                body = self.rfile.read(length)
                encoding = self.headers.get("Content-Encoding", "").lower()
                if encoding == "gzip":
                    body = gzip.decompress(body)
                elif encoding:
                    raise ValueError
                if self.path not in types:
                    self.send_response(404)
                    record_error("unexpected_export_path")
                elif self.headers.get_all("Authorization", []) != [f"Bearer {TOKEN}"]:
                    self.send_response(401)
                    record_error("unexpected_export_auth")
                elif self.headers.get_content_type() != "application/x-protobuf":
                    self.send_response(415)
                    record_error("unexpected_export_content_type")
                else:
                    message = types[self.path]()
                    message.ParseFromString(body)
                    with changed:
                        received[self.path].append(message)
                        changed.notify_all()
                    self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except Exception:
                record_error("invalid_export_request")
                self.send_error(400)

    used_ports = set()
    sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    used_ports.add(sink.server_port)
    sink.daemon_threads = True
    server_thread = threading.Thread(target=sink.serve_forever, daemon=True)
    server_thread.start()
    process = None
    opener = build_opener(ProxyHandler({}))  # Do not honor proxy environment variables.

    try:
        with tempfile.TemporaryDirectory(prefix="collector-check-", dir="/tmp") as temp, ExitStack() as owned:
            directory = Path(temp)
            receiver, health, metrics = (free_port(used_ports) for _ in range(3))
            process_file = directory / "process-output.jsonl"
            supervisor_file = directory / "supervisor.log"
            now = time.time_ns()
            seconds, nanos = divmod(now, 1_000_000_000)
            process_file.write_text(
                json.dumps(
                    {
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{nanos:09d}Z",
                        "severity": "INFO",
                        "service_name": "collector-check-process",
                        "service_version": "fixture-revision",
                        "service_instance": "fixture-process-1",
                        "body": "process fixture Bearer fixture-private-process-token",
                        "attributes": {"fixture.source": "process-jsonl"},
                    },
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            supervisor_file.write_text(
                time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(seconds))
                + ",000 INFO supervisor fixture Bearer fixture-private-supervisor-token\n",
                encoding="utf-8",
            )

            config = rewrite(
                config_path.read_text(encoding="utf-8"),
                [
                    ("/data/telemetry/collector-checkpoints", str(directory / "checkpoints"), "checkpoint path"),
                    ("include: [/var/log/relay-telemetry/process-output.jsonl, /var/log/relay-telemetry/process-output.jsonl.1]",
                     f"include: [{process_file}, {process_file}.1]", "process filelog paths"),
                    ("/var/log/supervisor/relay-supervisor.log", str(supervisor_file), "supervisor filelog path"),
                    ("127.0.0.1:4318", f"127.0.0.1:{receiver}", "OTLP receiver"),
                    ("127.0.0.1:13133", f"127.0.0.1:{health}", "health endpoint"),
                    ("port: 8888", f"port: {metrics}", "internal metrics endpoint"),
                    (
                        "endpoint: https://openwoof.javadevjt.tech",
                        f"endpoint: http://127.0.0.1:{sink.server_port}",
                        "provider exporter endpoint",
                    ),
                ],
            )
            if "openwoof.javadevjt.tech" in config:
                raise CheckFailure("fixture config retained the provider endpoint")
            fixture_config = directory / "collector.yaml"
            fixture_config.write_text(config, encoding="utf-8")
            credential = directory / "fixture-ingest-key"
            credential.write_text(TOKEN, encoding="ascii")
            credential.chmod(0o600)

            env = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(directory),
                "TMPDIR": str(directory),
                "TZ": "UTC",
                "OPENWOOF_INGEST_KEY_FILE": str(credential),
                "RELAY_SOURCE_REVISION": "fixture-revision",
            }
            process = subprocess.Popen(
                [str(executable.resolve()), "--config=" + str(fixture_config)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=directory,
                env=env,
                close_fds=True,
            )
            def stop_process():
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
            owned.callback(stop_process)
            wait_for_health(process, health, opener)

            trace = ParseDict(
                {
                    "resourceSpans": [
                        {
                            "scopeSpans": [
                                {
                                    "scope": {"name": "collector-check"},
                                    "spans": [
                                        {
                                            "traceId": base64.b64encode(b"\x11" * 16).decode(),
                                            "spanId": base64.b64encode(b"\x22" * 8).decode(),
                                            "name": "collector.check.synthetic",
                                            "kind": "SPAN_KIND_INTERNAL",
                                            "startTimeUnixNano": str(now),
                                            "endTimeUnixNano": str(now + 1_000_000),
                                            "attributes": [
                                                {
                                                    "key": "http.request.header.authorization",
                                                    "value": {"stringValue": "Bearer fixture-private-header-token"},
                                                },
                                                {
                                                    "key": "fixture.auth",
                                                    "value": {"stringValue": "Bearer fixture-private-span-token"},
                                                },
                                            ],
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                },
                ExportTraceServiceRequest(),
            )
            metric = ParseDict(
                {
                    "resourceMetrics": [
                        {
                            "scopeMetrics": [
                                {
                                    "scope": {"name": "collector-check"},
                                    "metrics": [
                                        {
                                            "name": "collector.check.synthetic",
                                            "unit": "1",
                                            "gauge": {"dataPoints": [{"timeUnixNano": str(now), "asDouble": 7.0}]},
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                },
                ExportMetricsServiceRequest(),
            )
            send_otlp(receiver, "/v1/traces", trace, opener)
            send_otlp(receiver, "/v1/metrics", metric, opener)

            deadline = time.monotonic() + 20
            with changed:
                while not errors and not all(received[path] for path in PATHS):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CheckFailure("all three signal paths did not arrive within 20 seconds")
                    changed.wait(min(remaining, 0.25))
                if errors:
                    raise CheckFailure("loopback OTLP exporter rejected a request")
                batches = {path: list(messages) for path, messages in received.items()}

            spans = [s for m in batches["/v1/traces"] for r in m.resource_spans for q in r.scope_spans for s in q.spans]
            metrics_out = [x for m in batches["/v1/metrics"] for r in m.resource_metrics for q in r.scope_metrics for x in q.metrics]
            records = [
                (r.resource, item)
                for m in batches["/v1/logs"]
                for r in m.resource_logs
                for q in r.scope_logs
                for item in q.log_records
            ]
            span = next((s for s in spans if s.name == "collector.check.synthetic"), None)
            if span is None or not any(x.name == "collector.check.synthetic" for x in metrics_out):
                raise CheckFailure("synthetic span or metric did not reach the exporter")
            attrs = attr_strings(span.attributes)
            if attrs.get("fixture.auth") != "Bearer [REDACTED]" or "http.request.header.authorization" in attrs:
                raise CheckFailure("source privacy transform did not redact the synthetic span attributes")
            trace_wire = b"".join(m.SerializeToString() for m in batches["/v1/traces"])
            if any(secret in trace_wire for secret in (b"fixture-private-header-token", b"fixture-private-span-token")):
                raise CheckFailure("synthetic span credential reached the exporter")

            process_records = [
                record for resource, record in records
                if attr_strings(resource.attributes).get("service.name") == "collector-check-process"
            ]
            supervisor_records = [
                record for resource, record in records
                if attr_strings(resource.attributes).get("service.name") == "discord-option-tailer-runtime"
            ]
            if not process_records or not supervisor_records:
                raise CheckFailure("process JSONL and supervisor filelog records were not both exported")
            log_wire = b"".join(m.SerializeToString() for m in batches["/v1/logs"])
            if any(secret in log_wire for secret in (b"fixture-private-process-token", b"fixture-private-supervisor-token")):
                raise CheckFailure("synthetic log credential reached the exporter")
            for record in process_records + supervisor_records:
                if "Bearer [REDACTED]" not in record.body.string_value:
                    raise CheckFailure("source privacy transform did not redact a synthetic log")

            print("PASS: synthetic span and metric traversed the OTLP receiver and pipelines")
            print("PASS: process JSONL and supervisor filelog records reached the fake OTLP exporter")
            print("PASS: source privacy redaction and fixture-only exporter authorization")
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        sink.shutdown()
        sink.server_close()
        server_thread.join(timeout=2)


if __name__ == "__main__":
    try:
        main()
    except CheckFailure as exc:
        raise SystemExit(f"collector pipeline check failed: {exc}") from None
