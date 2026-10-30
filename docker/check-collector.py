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
    for replacement in replacements:
        old, new, label = replacement[:3]
        expected = replacement[3] if len(replacement) == 4 else 1
        if config.count(old) != expected:
            raise CheckFailure(f"production config no longer has one expected {label}")
        config = config.replace(old, new, expected)
    return config


def attr_strings(attributes):
    return {
        item.key: item.value.string_value
        for item in attributes
        if item.value.WhichOneof("value") == "string_value"
    }


def wait_for_health(process, port, opener, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise CheckFailure("Collector exited before its health endpoint became ready")
        try:
            with opener.open(f"http://127.0.0.1:{port}/", timeout=0.5):
                return
        except (OSError, URLError):
            time.sleep(0.1)
    raise CheckFailure(f"Collector did not become ready within {timeout:g} seconds")


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
    attempts = []
    errors = []
    changed = threading.Condition()
    blocked = threading.Event()

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
                message = None
                status = None
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
                    status = 429 if blocked.is_set() else 200
                    self.send_response(status)
                    if status == 429:
                        self.send_header("Retry-After", "1")
                self.send_header("Content-Length", "0")
                self.end_headers()
                if status in (200, 429):
                    with changed:
                        attempts.append((self.path, status, message))
                        if status == 200:
                            received[self.path].append(message)
                        changed.notify_all()
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
                    ("directory: /data/telemetry/collector-queue/compaction",
                     f"directory: {directory / 'queue-compaction'}", "queue compaction path"),
                    ("directory: /data/telemetry/collector-queue",
                     f"directory: {directory / 'queue'}", "queue storage path"),
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
                        3,
                    ),
                ],
            )
            if "openwoof.javadevjt.tech" in config:
                raise CheckFailure("fixture config retained the provider endpoint")
            if "  extensions: [bearertokenauth/openwoof, file_storage, file_storage/queue, health_check]" not in config:
                raise CheckFailure("persistent queue storage extension is not enabled")
            if config.count("      storage: file_storage/queue") != 3:
                raise CheckFailure("all signal exporters must use persistent queue storage")
            for text, count in (
                ("      queue_size: 16777216", 3),
                ("      num_consumers: 1", 3),
                ("      sizer: bytes", 6),
                ("      block_on_overflow: false", 3),
                ("      wait_for_result: false", 3),
                ("      max_elapsed_time: 0s", 3),
            ):
                if config.count(text) != count:
                    raise CheckFailure(f"signal queue config does not meet resilience requirement: {text.strip()}")
            if any(f"exporters: [otlp_http/openwoof_{signal}]" not in config for signal in ("traces", "metrics", "logs")):
                raise CheckFailure("each signal must use its isolated exporter queue")
            if "/data/telemetry/collector-queue" in config:
                raise CheckFailure("fixture config retained the production queue path")
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

            # Exercise actual queue persistence with a synthetic upstream 429,
            # abrupt Collector loss, and recovery from the same storage directory.
            marker = "collector.check.restart-persisted"
            blocked.set()
            restart_trace = ParseDict(
                {
                    "resourceSpans": [
                        {
                            "scopeSpans": [
                                {
                                    "scope": {"name": "collector-check"},
                                    "spans": [
                                        {
                                            "traceId": base64.b64encode(b"\x33" * 16).decode(),
                                            "spanId": base64.b64encode(b"\x44" * 8).decode(),
                                            "name": marker,
                                            "kind": "SPAN_KIND_INTERNAL",
                                            "startTimeUnixNano": str(now + 2),
                                            "endTimeUnixNano": str(now + 3),
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                },
                ExportTraceServiceRequest(),
            )
            restart_metric = ParseDict(
                {
                    "resourceMetrics": [
                        {
                            "scopeMetrics": [
                                {
                                    "scope": {"name": "collector-check"},
                                    "metrics": [
                                        {
                                            "name": marker,
                                            "unit": "1",
                                            "gauge": {"dataPoints": [{"timeUnixNano": str(now + 2), "asDouble": 1.0}]},
                                        }
                                    ],
                                }
                            ]
                        }
                    ]
                },
                ExportMetricsServiceRequest(),
            )
            send_otlp(receiver, "/v1/traces", restart_trace, opener)
            send_otlp(receiver, "/v1/metrics", restart_metric, opener)
            process_file.write_text(
                process_file.read_text(encoding="utf-8")
                + json.dumps(
                    {
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{nanos:09d}Z",
                        "severity": "INFO",
                        "service_name": "collector-check-process",
                        "service_version": "fixture-revision",
                        "service_instance": "fixture-process-1",
                        "body": marker + " Bearer fixture-private-restart-token",
                        "attributes": {"fixture.source": "process-jsonl"},
                    },
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )

            def has_marker(path, message):
                if path == "/v1/traces":
                    return any(
                        span.name == marker
                        for resource in message.resource_spans
                        for scope in resource.scope_spans
                        for span in scope.spans
                    )
                if path == "/v1/metrics":
                    return any(
                        metric.name == marker
                        for resource in message.resource_metrics
                        for scope in resource.scope_metrics
                        for metric in scope.metrics
                    )
                return any(
                    marker in record.body.string_value
                    for resource in message.resource_logs
                    for scope in resource.scope_logs
                    for record in scope.log_records
                )

            deadline = time.monotonic() + 20
            with changed:
                while not all(
                    any(path == expected and status == 429 and has_marker(path, message)
                        for path, status, message in attempts)
                    for expected in PATHS
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CheckFailure("synthetic marker did not receive 429 on all three signal queues")
                    changed.wait(min(remaining, 0.25))
                if errors:
                    raise CheckFailure("loopback 429 fixture rejected a synthetic request")

            # Allow the 429 response to finish before killing the collector so the
            # failed request remains pending in the persistent queue.
            time.sleep(0.1)
            process.kill()
            process.wait(timeout=3)
            blocked.clear()
            process = subprocess.Popen(
                [str(executable.resolve()), "--config=" + str(fixture_config)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=directory,
                env=env,
                close_fds=True,
            )
            wait_for_health(process, health, opener)

            deadline = time.monotonic() + 20
            with changed:
                while not all(
                    any(has_marker(path, message) for message in received[path])
                    for path in PATHS
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CheckFailure("queued synthetic data did not recover after Collector restart")
                    changed.wait(min(remaining, 0.25))
                if errors:
                    raise CheckFailure("loopback recovery fixture rejected a synthetic request")
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
            if not any(s.name == marker for s in spans) or not any(m.name == marker for m in metrics_out):
                raise CheckFailure("a queued trace or metric was not recovered after Collector restart")
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
            if any(secret in log_wire for secret in (b"fixture-private-process-token", b"fixture-private-supervisor-token", b"fixture-private-restart-token")):
                raise CheckFailure("synthetic log credential reached the exporter")
            if not any(marker in record.body.string_value for record in process_records):
                raise CheckFailure("queued log was not recovered after Collector restart")
            for record in process_records + supervisor_records:
                if "Bearer [REDACTED]" not in record.body.string_value:
                    raise CheckFailure("source privacy transform did not redact a synthetic log")

            print("PASS: synthetic span and metric traversed the OTLP receiver and pipelines")
            print("PASS: process JSONL and supervisor filelog records reached the fake OTLP exporter")
            print("PASS: source privacy redaction and fixture-only exporter authorization")
            print("PASS: synthetic 429 then abrupt restart recovered queued traces, metrics, and logs")
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
