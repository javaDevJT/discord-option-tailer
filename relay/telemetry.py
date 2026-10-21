"""Optional, bounded telemetry. No payloads, credentials, orders, or network QA.

The application SDK sends only to the same container's private Collector. The
Collector alone owns the upstream credential. Importing this module is inert.
"""
from contextlib import contextmanager
import atexit
import functools
import inspect
import os
import sys
import time
from urllib.parse import urlsplit

SERVICE_NAMES = {
    "dashboard": "discord-option-tailer-dashboard",
    "service": "discord-option-tailer-worker",
    "notifications": "discord-option-tailer-notifications",
}
OPERATIONS = frozenset({
    "robinhood.tool", "robinhood.initialize", "robinhood.discover", "relay.message", "relay.evaluate",
    "codex.process", "discord.receive", "relay.account.refresh",
    "relay.reconcile", "relay.position.monitor", "relay.notify",
})
SQL_OPERATIONS = frozenset("sqlite." + verb for verb in (
    "SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "PRAGMA",
    "BEGIN", "COMMIT", "ROLLBACK", "EXPLAIN", "REPLACE", "WITH", "SQL",
))
DASHBOARD_ROUTES = frozenset({
    "/", "/healthz", "/api/status", "/api/setup", "/api/account", "/api/telemetry",
    "/api/messages", "/api/message-image", "/api/orders", "/api/events", "/api/positions",
    "/api/monitors", "/api/evaluation/metrics", "/api/setup/robinhood/schemas",
    "/api/setup/channels", "/api/setup/pause", "/api/setup/reconnect", "/api/setup/mode",
    "/api/setup/notifications", "/api/setup/expiry-policy", "/api/setup/evaluation",
    "/api/setup/evaluation/test", "/api/setup/discord", "/api/setup/discord/discover",
    "/api/setup/auth/codex/start", "/api/setup/auth/codex/cancel",
    "/api/setup/auth/robinhood/start", "/api/setup/auth/robinhood/cancel",
    "/api/setup/auth/robinhood/callback",
})
_runtime = None


def enabled():
    return os.environ.get("RELAY_OTEL_ENABLED", "").lower() in {"1", "true"}


def initialize(program):
    """Initialize once, before loading application modules; leave foreign providers intact."""
    global _runtime
    if _runtime is not None or not enabled():
        return _runtime
    if program not in SERVICE_NAMES:
        raise ValueError("unsupported telemetry program")
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.metrics.view import View
        from opentelemetry.sdk.trace import SpanLimits

        # An existing provider belongs to its installer; never install a second copy.
        if not isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
            return None
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
        destination = urlsplit(endpoint)
        if (destination.scheme != "http" or destination.hostname != "127.0.0.1"
                or destination.username or destination.password or destination.query
                or destination.fragment or destination.path or not destination.port):
            raise ValueError("SDK endpoint must be the private same-container Collector")
        for key in tuple(os.environ):
            if key.startswith("OTEL_EXPORTER_OTLP_") and key != "OTEL_EXPORTER_OTLP_ENDPOINT":
                os.environ.pop(key)
            if key.startswith("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS"):
                os.environ.pop(key)
        os.environ["OTEL_EXPORTER_OTLP_PROTOCOL"] = "http/protobuf"
        os.environ["OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"] = "CUMULATIVE"
        os.environ["OTEL_LOGS_EXPORTER"] = "none"  # Original logs have one Supervisor/filelog path.
        resource = Resource.create({
            "service.name": SERVICE_NAMES[program],
            "service.namespace": "trading",
            "deployment.environment.name": "production",
            "service.version": os.environ.get("RELAY_SOURCE_REVISION", "unknown"),
            "service.instance.id": f"{os.environ.get('HOSTNAME', 'local')}:{program}:{os.getpid()}",
            "process.pid": os.getpid(),
        })
        tracer_provider = TracerProvider(resource=resource, span_limits=SpanLimits(
            max_attributes=48, max_events=16, max_links=8, max_attribute_length=1024,
        ), shutdown_on_exit=False)
        tracer_provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint + "/v1/traces", headers={}, timeout=2),
            max_queue_size=1024, max_export_batch_size=128,
            schedule_delay_millis=1000, export_timeout_millis=2000,
        ))
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=endpoint + "/v1/metrics", headers={}, timeout=2),
            export_interval_millis=30000, export_timeout_millis=2000,
        )
        meter_provider = MeterProvider(resource=resource, metric_readers=[reader],
            views=[View(instrument_name="*", attribute_keys={
                "operation", "outcome", "http.request.method", "http.route",
                "http.response.status_code", "network.protocol.name",
            })],
            shutdown_on_exit=False)
        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(meter_provider)
        tracer = trace.get_tracer("relay.operations", "1")
        meter = metrics.get_meter("relay.operations", "1")
        calls = meter.create_counter("relay.operation.calls", unit="{call}",
            description="Completed observed application operations, including errors and cancellation")
        duration = meter.create_histogram("relay.operation.duration", unit="s",
            description="Observed operation elapsed time, excluding telemetry export")
        http_duration = meter.create_histogram("http.server.request.duration", unit="s")
        started = time.monotonic()

        def uptime(_options):
            yield metrics.Observation(time.monotonic() - started)

        meter.create_observable_gauge("relay.process.uptime", callbacks=[uptime], unit="s")
        _runtime = {"tracer": tracer, "calls": calls, "duration": duration,
            "http_duration": http_duration, "providers": (tracer_provider, meter_provider),
            "closed": False}
        atexit.register(shutdown)
        _instrument_dependencies()
        return _runtime
    except Exception as exc:
        # Telemetry must not become an application availability dependency.
        print(f"OpenWoof telemetry initialization failed [{type(exc).__name__}]; original application output remains enabled.", file=sys.stderr)
        return None


def _instrument_dependencies():
    import importlib.util
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
    from opentelemetry.instrumentation.urllib import URLLibInstrumentor
    # Standard instrumentation observes genuine network/cursor calls. No headers or
    # parameters are captured; Collector privacy processing removes URL queries/SQL.
    HTTPXClientInstrumentor().instrument()
    SQLite3Instrumentor().instrument()
    from .telemetry_sqlite import instrument as instrument_shortcuts
    instrument_shortcuts()
    URLLibInstrumentor().instrument()
    if importlib.util.find_spec("aiohttp") is not None:
        from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor
        AioHttpClientInstrumentor().instrument()


class NullSpan:
    def set_attribute(self, *_args, **_kwargs):
        pass


class ObservedSpan:
    """Keep outcome metadata even when an upstream sampling flag drops the span."""
    def __init__(self, span):
        self.span, self.failed = span, False

    def set_attribute(self, *args, **kwargs):
        try:
            self.span.set_attribute(*args, **kwargs)
        except Exception:
            pass

    def set_status(self, status):
        from opentelemetry.trace import StatusCode
        self.failed = status.status_code == StatusCode.ERROR
        try:
            self.span.set_status(status)
        except Exception:
            pass


@contextmanager
def operation(name, *, kind="internal", attributes=None, carrier=None):
    runtime = _runtime
    if runtime is None or runtime["closed"]:
        yield NullSpan()
        return
    from opentelemetry import trace
    from opentelemetry.propagate import extract
    from opentelemetry.trace import SpanKind, Status, StatusCode
    if name not in OPERATIONS and name not in SQL_OPERATIONS and not name.startswith("HTTP "):
        raise ValueError("telemetry operation must have a bounded name")
    attributes = dict(attributes or {})
    span_kind = {"client": SpanKind.CLIENT, "server": SpanKind.SERVER,
        "consumer": SpanKind.CONSUMER}.get(kind, SpanKind.INTERNAL)
    context = extract(carrier) if carrier is not None else None
    started, outcome = time.perf_counter(), "success"
    with runtime["tracer"].start_as_current_span(name, kind=span_kind,
            attributes=attributes, context=context,
            record_exception=False, set_status_on_exception=False) as span:
        observed = ObservedSpan(span)
        try:
            yield observed
        except BaseException as exc:
            outcome = "cancelled" if type(exc).__name__ in {"CancelledError", "KeyboardInterrupt"} else "error"
            span.set_attribute("error.type", type(exc).__name__)
            span.set_status(Status(StatusCode.ERROR))
            raise
        finally:
            if observed.failed:
                outcome = "error" if outcome == "success" else outcome
            elapsed = time.perf_counter() - started
            labels = {"operation": name, "outcome": outcome}
            try:
                runtime["calls"].add(1, labels)
                runtime["duration"].record(elapsed, labels)
                if kind == "server":
                    runtime["http_duration"].record(elapsed, {
                        k: v for k, v in attributes.items()
                        if k in {"http.request.method", "http.route"}
                    })
            except Exception:
                pass  # Metrics cannot replace an application's exception or return.


def mark_error(span, error_type):
    if isinstance(span, NullSpan):
        return
    from opentelemetry.trace import Status, StatusCode
    span.set_attribute("error.type", error_type)
    span.set_status(Status(StatusCode.ERROR))


def traced(name, *, kind="internal", attributes=None):
    """Wrap genuine application operations without recording arguments or payloads."""
    def decorate(function):
        def result_metadata(span, result):
            if isinstance(result, dict):
                state = result.get("state")
                if isinstance(state, str) and len(state) < 40:
                    span.set_attribute("app.result.state", state)
                if state in {"error", "failed", "eval_failed"}:
                    mark_error(span, "ApplicationResultError")
            if hasattr(result, "returncode") and isinstance(result.returncode, int):
                span.set_attribute("process.exit.code", result.returncode)
                if result.returncode:
                    mark_error(span, "ProcessExitError")
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def asynchronous(*args, **kwargs):
                with operation(name, kind=kind, attributes=attributes) as span:
                    result = await function(*args, **kwargs)
                    result_metadata(span, result)
                    return result
            return asynchronous
        @functools.wraps(function)
        def synchronous(*args, **kwargs):
            with operation(name, kind=kind, attributes=attributes) as span:
                result = function(*args, **kwargs)
                result_metadata(span, result)
                return result
        return synchronous
    return decorate


@contextmanager
def http_request(handler):
    path = urlsplit(handler.path).path
    route = path if path in DASHBOARD_ROUTES else "/static/*" if path.startswith("/static/") else "/other"
    if route in {"/healthz", "/api/status", "/api/telemetry"}:
        yield NullSpan()
        return
    attributes = {"http.request.method": handler.command, "http.route": route,
        "network.protocol.name": "http", "network.protocol.version": "1.1"}
    carrier = {key: handler.headers.get(key, "") for key in ("traceparent", "tracestate")}
    with operation("HTTP " + handler.command + " " + route, kind="server",
            attributes=attributes, carrier=carrier) as span:
        handler._telemetry_request_span = span
        try:
            yield span
        finally:
            handler._telemetry_request_span = None


def shutdown():
    runtime = _runtime
    if runtime is None or runtime["closed"]:
        return
    runtime["closed"] = True
    for provider in runtime["providers"]:
        try:
            provider.force_flush(timeout_millis=2000)
            provider.shutdown()
        except Exception:
            print("OpenWoof telemetry shutdown could not flush a batch.", file=sys.stderr)


def status():
    """Authenticated dashboard diagnostics; fixed loopback URLs, never key contents."""
    import re
    import urllib.request
    result = {"enabled": enabled(), "sdk_initialized": _runtime is not None,
        "source_revision": os.environ.get("RELAY_SOURCE_REVISION", "unknown"),
        "collector_healthy": False, "collector_metrics": {}}
    if not result["enabled"]:
        return result
    try:
        with urllib.request.urlopen("http://127.0.0.1:13133/", timeout=0.5) as response:
            result["collector_healthy"] = response.status == 200
        with urllib.request.urlopen("http://127.0.0.1:8888/metrics", timeout=0.5) as response:
            text = response.read(1024 * 1024).decode("utf-8")
        for line in text.splitlines():
            match = re.fullmatch(r"(otelcol_(?:exporter|receiver|processor)_[a-z_]+)(\{[^\n]*\})? ([0-9.eE+-]+)", line)
            if match:
                name, labels, value = match.groups()
                # These are raw current-process cumulative counters/gauges, not
                # invented rates or sums across resets/export observations.
                result["collector_metrics"][name + (labels or "")] = float(value)
    except (OSError, ValueError):
        result["collector_error"] = "collector_unavailable_or_metrics_unreadable"
    return result
