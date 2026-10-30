#!/usr/bin/env python3
"""Isolated SDK -> pinned Collector -> TLS OTLP fixture. Never contacts a provider.

Run with --collector <official local binary> --output <new private directory>.
All credentials/messages below are test fixtures; this is not production proof.
"""
import argparse
import gzip
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collector", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    directory = args.output.resolve()
    received, lock, reject = [], threading.Lock(), threading.Event()
    release_traces = threading.Event()
    release_traces.set()

    class Sink(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            auth = self.headers.get_all("Authorization", [])
            assert auth == ["Bearer fixture-private-ingest"]
            if self.path == "/v1/traces":
                release_traces.wait(4)
            with lock: received.append((self.path, body, reject.is_set()))
            self.send_response(503 if reject.is_set() else 200)
            self.send_header("Content-Length", "0"); self.end_headers()

    class MarketFixture(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")

    cert, key = directory / "cert.pem", directory / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
        "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    key.chmod(0o600)
    sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); tls.load_cert_chain(cert, key)
    sink.socket = tls.wrap_socket(sink.socket, server_side=True)
    market = ThreadingHTTPServer(("127.0.0.1", 0), MarketFixture)
    for server in (sink, market): threading.Thread(target=server.serve_forever, daemon=True).start()
    receiver_port, health_port, metrics_port = port(), port(), port()
    credential = directory / "fixture-key"
    credential.write_text("fixture-private-ingest"); credential.chmod(0o600)
    config = (ROOT / "docker/otelcol.yaml").read_text()
    for old, new in {
        "/data/telemetry/collector-checkpoints": str(directory / "checkpoints"),
        "/data/telemetry/collector-queue/compaction": str(directory / "queue-compaction"),
        "/data/telemetry/collector-queue": str(directory / "queue"),
        "/var/log/relay-telemetry/process-output.jsonl": str(directory / "process-output.jsonl"),
        "/var/log/supervisor/relay-supervisor.log": str(directory / "supervisor.log"),
        "127.0.0.1:4318": f"127.0.0.1:{receiver_port}",
        "127.0.0.1:13133": f"127.0.0.1:{health_port}",
        "port: 8888": f"port: {metrics_port}",
        "endpoint: https://openwoof.javadevjt.tech": f"endpoint: https://127.0.0.1:{sink.server_port}",
        "      insecure: false": f"      ca_file: {cert}\n      insecure: false",
    }.items(): config = config.replace(old, new)
    if "openwoof.javadevjt.tech" in config or "/data/telemetry/collector-queue" in config:
        raise RuntimeError("fixture config retained a production endpoint or persistent storage path")
    configuration = directory / "collector.yaml"; configuration.write_text(config)
    env = dict(os.environ, OPENWOOF_INGEST_KEY_FILE=str(credential), RELAY_SOURCE_REVISION="fixture-20261021")
    actor = r'''
import json,os,sqlite3,sys,time
import httpx
from relay.telemetry import initialize,operation,shutdown,mark_error
from relay.telemetry_logs import Framer,RingWriter
assert initialize("service") is not None
with operation("relay.evaluate",carrier={"traceparent":"00-"+"1"*32+"-"+"2"*16+"-00"}) as unsampled:
    mark_error(unsampled,"ControlledUnsampledResult")
with operation("relay.message",kind="consumer"):
    db=sqlite3.connect(":memory:");db.execute("CREATE TABLE example(value TEXT)")
    db.execute("INSERT INTO example VALUES (?)",("fixture-payload-not-captured",))
    assert db.execute("SELECT value FROM example").fetchone()[0]=="fixture-payload-not-captured"
    httpx.get(os.environ["FIXTURE_MARKET"]+"/book?limit=100&api_key=fixture-private-query").raise_for_status()
    try:
        with operation("relay.evaluate"):
            raise ValueError("controlled failure")
    except ValueError: pass
body=json.dumps({"state":"held","at":"2026-10-21T18:00:00+00:00","reason":"Retain quoted ‘detail’ and multiline\nsecond line","password":"fixture-private-body"},indent=2,ensure_ascii=False)
print(body,flush=True)
framer=Framer(RingWriter(os.environ["FIXTURE_DIRECTORY"]))
framer.feed("service",str(os.getpid()),"stdout",(body+"\n").encode());framer.flush()
shutdown()
'''
    burst_actor = r'''
import json,os
from relay.telemetry import initialize,operation,shutdown
runtime=initialize("service")
for batch in range(24):
    for item in range(64):
        with operation("relay.evaluate"):
            value=batch*64+item
            assert value>=0
    assert runtime["providers"][0].force_flush(timeout_millis=2000)
shutdown()
print(json.dumps({"fixture_operations":1536}))
'''
    log = (directory / "collector.log").open("wb")
    process = subprocess.Popen([args.collector, "--config=" + str(configuration)], env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        import urllib.request
        deadline = time.monotonic() + 15
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{health_port}/", timeout=0.5).close(); break
            except OSError:
                if time.monotonic() > deadline: raise RuntimeError("fixture Collector did not start")
                time.sleep(0.1)
        actor_env = dict(os.environ, RELAY_OTEL_ENABLED="true", OTEL_EXPORTER_OTLP_ENDPOINT=f"http://127.0.0.1:{receiver_port}", RELAY_SOURCE_REVISION="fixture-20261021", FIXTURE_MARKET=f"http://127.0.0.1:{market.server_port}", FIXTURE_DIRECTORY=str(directory))
        first = subprocess.run([sys.executable, "-c", actor], env=actor_env, cwd=ROOT, capture_output=True, timeout=20)
        (directory / "actor.stdout").write_bytes(first.stdout); (directory / "actor.stderr").write_bytes(first.stderr)
        assert first.returncode == 0, "fixture application failed"
        assert b"fixture-private-body" in first.stdout, "original application output changed"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if {path for path, _, failed in received if not failed} == {"/v1/traces", "/v1/metrics", "/v1/logs"}: break
            time.sleep(0.1)
        spans, logs, metrics = [], [], []
        for path, body, failed in list(received):
            if failed: continue
            if path == "/v1/traces":
                message = ExportTraceServiceRequest.FromString(body)
                spans.extend(span for resource in message.resource_spans for scope in resource.scope_spans for span in scope.spans)
            elif path == "/v1/logs":
                message = ExportLogsServiceRequest.FromString(body)
                logs.extend((resource, scope, record) for resource in message.resource_logs for scope in resource.scope_logs for record in scope.log_records)
            elif path == "/v1/metrics":
                message = ExportMetricsServiceRequest.FromString(body)
                metrics.extend(metric for resource in message.resource_metrics for scope in resource.scope_metrics for metric in scope.metrics)
        assert spans and logs and metrics, "three-signal fixture chain incomplete"
        assert any(span.name == "relay.message" for span in spans)
        assert any(span.name == "relay.evaluate" and span.status.code == 2 for span in spans)
        assert any(span.parent_span_id and span.kind == 3 for span in spans), "no actual dependency child span"
        identities = {(span.trace_id, span.span_id) for span in spans}
        assert any((span.trace_id, span.parent_span_id) in identities and span.kind == 3 for span in spans), "dependency parent is not observed"
        assert any(any(a.key in {"db.statement", "db.query.text"} for a in s.attributes) for s in spans), "shortcut SQLite operations were not instrumented"
        encoded = b"\n".join(body for _, body, failed in received if not failed)
        for secret in [b"fixture-private-query", b"fixture-private-body", b"fixture-payload-not-captured"]:
            assert secret not in encoded, "private fixture content escaped filtering"
        assert b"limit=100" in encoded, "benign query context was lost"
        resource, scope, original = next((r,s,l) for r,s,l in logs if "Retain quoted" in l.body.string_value)
        assert "‘detail’" in original.body.string_value and "second line" in original.body.string_value
        assert "[REDACTED]" in original.body.string_value and original.severity_number == 9
        assert original.time_unix_nano == 1792605600000000000
        assert "filelog" in scope.scope.name
        assert any(a.key == "log.logger.name" and a.value.string_value == "service.stdout" for a in original.attributes)
        assert original.trace_id == b"" or int.from_bytes(original.trace_id,"big") == 0  # untraced output is retained
        names = {metric.name: metric for metric in metrics}
        assert names["relay.operation.calls"].unit == "{call}"
        assert names["relay.operation.calls"].sum.is_monotonic
        assert names["relay.operation.calls"].sum.aggregation_temporality == 2
        assert names["relay.operation.duration"].unit == "s"
        assert names["relay.operation.duration"].histogram.aggregation_temporality == 2
        assert names["relay.process.uptime"].HasField("gauge")
        # Reproduce a brief upstream stall without slowing the application or
        # generating any production work. Every expected span must arrive.
        def received_span_count():
            with lock:
                batches = list(received)
            return sum(len(scope.spans) for path, body, failed in batches
                       if path == "/v1/traces" and not failed
                       for resource in ExportTraceServiceRequest.FromString(body).resource_spans
                       for scope in resource.scope_spans)
        before_burst = received_span_count()
        release_traces.clear()
        try:
            burst = subprocess.run([sys.executable, "-c", burst_actor], env=actor_env,
                                   cwd=ROOT, capture_output=True, timeout=20)
            (directory / "burst.stdout").write_bytes(burst.stdout)
            (directory / "burst.stderr").write_bytes(burst.stderr)
            assert burst.returncode == 0, "isolated burst actor failed"
        finally:
            release_traces.set()
        deadline = time.monotonic() + 12
        while received_span_count() < before_burst + 1536 and time.monotonic() < deadline:
            time.sleep(0.1)
        assert received_span_count() == before_burst + 1536, "Collector dropped spans during the controlled upstream stall"
        with urllib.request.urlopen(f"http://127.0.0.1:{metrics_port}/metrics", timeout=2) as response:
            counters = response.read().decode()
        for metric in counters.splitlines():
            if metric.startswith("otelcol_exporter_enqueue_failed_"):
                assert float(metric.rsplit(" ", 1)[1]) == 0, "Collector enqueue loss observed"
        reject.set()
        second = subprocess.run([sys.executable, "-c", actor], env=actor_env, cwd=ROOT, capture_output=True, timeout=20)
        (directory / "failure-isolation.stdout").write_bytes(second.stdout)
        (directory / "failure-isolation.stderr").write_bytes(second.stderr)
        assert second.returncode == 0 and b"fixture-private-body" in second.stdout
        deadline = time.monotonic() + 5
        while not any(failed for _, _, failed in received) and time.monotonic() < deadline: time.sleep(0.1)
        assert any(failed for _, _, failed in received), "fixture did not exercise exporter failure"
        result = {"status":"PASS", "environment":"isolated-local-fixture", "production_coverage":False,
            "collector":"0.163.2", "spans":len(spans), "logs":len(logs), "metric_names":sorted(names),
            "success_failure_and_dependency_children":True, "original_output_preserved":True,
            "redaction_preserves_nonsecret_detail":True, "untraced_log_retained":True,
            "cumulative_kinds_units_temporality_checked":True, "upstream_failure_does_not_break_application":True}
        result["controlled_stall_burst_spans_delivered_without_enqueue_loss"] = 1536
        result["metric_metadata"] = [{"name":m.name,"unit":m.unit,"kind":m.WhichOneof("data"),
            "temporality":getattr(getattr(m,m.WhichOneof("data")),"aggregation_temporality",None)} for m in metrics]
        (directory / "result.json").write_text(json.dumps(result,indent=2)+"\n")
        print(json.dumps(result))
    finally:
        release_traces.set()
        process.terminate()
        try: process.wait(timeout=8)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
        log.close(); sink.shutdown(); market.shutdown()


if __name__ == "__main__": main()
