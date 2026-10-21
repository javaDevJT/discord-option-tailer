"""Supervisor's additive process-output tee. Original console routing is untouched.

One bounded JSONL copy feeds the app-scoped Collector filelog receiver. This
listener never holds an ingest key or does network IO. Pretty-printed JSON is
framed as a document; other output is explicitly newline-framed, including
tracebacks. Per-process sequence numbers preserve source ordering.
"""
import codecs
import ast
from datetime import datetime, timezone
import json
import io
import os
from pathlib import Path
import re
import signal
import sys
import time

ROOT = Path("/var/log/relay-telemetry")
MAX_FRAME = 65536
MAX_PENDING = 2 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024
SERVICES = {
    "dashboard": "discord-option-tailer-dashboard", "service": "discord-option-tailer-worker",
    "notifications": "discord-option-tailer-notifications", "nginx": "discord-option-tailer-gateway",
    "xvfb": "discord-option-tailer-desktop", "openbox": "discord-option-tailer-desktop",
    "x11vnc": "discord-option-tailer-desktop", "websockify": "discord-option-tailer-desktop",
}
SECRET_JSON = re.compile(r'(?i)("(?:access_token|refresh_token|token|password|secret|api_key|authorization|authorization_code|cookie|account_number|account_id)"\s*:\s*)("(?:[^"\\]|\\.)*"|[0-9]+)')
BEARER = re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
PRIVATE_QUERY = re.compile(r'(?i)([?&](?:token|access_token|refresh_token|key|api_key|password|code|state|signature|sig|hm|ex|is|account_id|account_number|email)=)[^&\s"<>]*')
ASSIGNMENT = re.compile(r'(?i)\b(password|access_token|refresh_token|api_key|authorization)\s*=\s*[^\s,;]+')
LEVEL = re.compile(r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL):([\w.]+):")
ACCESS = re.compile(r'"(GET|HEAD|POST|PUT|DELETE|PATCH|OPTIONS) ([^ ?"]+)(?:\?[^ "]*)? HTTP/[^" ]+" (\d{3}) ')


def redact(text):
    text = SECRET_JSON.sub(r'\1"[REDACTED]"', text)
    text = BEARER.sub(r"\1 [REDACTED]", text)
    text = ASSIGNMENT.sub(r"\1=[REDACTED]", text)
    text = re.sub(r"(https?://)[^\s/@:]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"(/api/webhooks/)[^/\s?]+/[^/\s?\"<>]+", r"\1[REDACTED]/[REDACTED]", text)
    return PRIVATE_QUERY.sub(r"\1[REDACTED]", text)


def record(program, pid, channel, body, sequence, framing, observed=None):
    if program not in SERVICES:
        return None  # Collector/listener diagnostics cannot recursively re-enter export.
    now = observed or datetime.now(timezone.utc).isoformat()
    attributes = {"process.program": program, "process.pid": int(pid),
        "log.iostream": channel, "log.sequence": sequence, "log.framing": framing,
        "log.time.source": "observed", "log.logger.name": f"{program}.{channel}",
        "log.severity.source": "stdout_default"}
    attributes["log.observed_at"] = now
    severity, event_time = "INFO", now
    try:
        document = json.loads(body)
    except (ValueError, TypeError):
        document = None
    if isinstance(document, dict):
        # Carry useful original diagnostic metadata without turning message/contract
        # identifiers, balances, contents, or raw payloads into resource/metric labels.
        for key in ("state", "event", "stage", "code", "action", "exception"):
            value = document.get(key)
            if isinstance(value, (str, bool, int, float)):
                attributes["app." + key] = value
        level = document.get("level", document.get("severity"))
        if isinstance(level, str) and level.upper() in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            severity = level.upper()
            attributes["log.severity.source"] = "original_level"
        elif document.get("state") in {"error", "failed", "eval_failed"}:
            severity = "ERROR"
            attributes["log.severity.source"] = "original_state"
        for key in ("timestamp", "at", "created_at"):
            value = document.get(key)
            if not isinstance(value, str):
                continue
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    event_time = parsed.isoformat()
                    attributes["log.time.source"] = "original_" + key
                    break
            except ValueError:
                pass
    match = LEVEL.match(body)
    if match:
        severity, attributes["log.logger.name"] = match.groups()
        attributes["log.severity.source"] = "original_formatted_level"
    access = ACCESS.search(body)
    if program == "nginx" and access:
        method, route, status = access.groups()
        if route in {"/healthz", "/api/status"} and int(status) < 400:
            return None  # Only successful routine health/readiness noise is omitted.
        severity = "ERROR" if int(status) >= 500 else "WARNING" if int(status) >= 400 else "INFO"
        attributes["http.request.method"] = method
        attributes["http.response.status_code"] = int(status)
        attributes["log.severity.source"] = "http_status"
        # The original access line stays intact apart from applicable private data.
        body = re.sub(r'^\S+ (?=\S+ \S+ \[)', "[REDACTED_CLIENT_ADDRESS] ", body)
    return {
        "timestamp": event_time, "observed_timestamp": now, "severity": severity,
        "body": redact(body), "attributes": attributes,
        "service_name": SERVICES[program], "service_version": os.environ.get("RELAY_SOURCE_REVISION", "unknown"),
        "service_instance": f"{os.environ.get('HOSTNAME', 'local')}:{program}:{pid}",
    }


class Framer:
    def __init__(self, emit):
        self.emit = emit
        self.pending, self.decoders, self.sequences, self.observed = {}, {}, {}, {}
        self.delimiters = set()

    def feed(self, program, pid, channel, chunk):
        key = (program, pid, channel)
        decoder = self.decoders.setdefault(key, codecs.getincrementaldecoder("utf-8")("replace"))
        text = decoder.decode(chunk)
        self.observed.setdefault(key, datetime.now(timezone.utc).isoformat())
        self.pending[key] = self.pending.get(key, "") + text
        self._drain(key)

    def _send(self, key, body, framing):
        # Redact the full logical record before splitting it. Otherwise a token
        # or JSON private field crossing an output segment could escape filtering.
        value = record(*key, body, self.sequences.get(key, 0) + 1, framing, self.observed.get(key))
        if value is not None:
            safe = value["body"]
            pieces = [safe[i:i + MAX_FRAME] for i in range(0, len(safe), MAX_FRAME)] or [""]
            for index, piece in enumerate(pieces):
                self.sequences[key] = self.sequences.get(key, 0) + 1
                attributes = dict(value["attributes"], **{"log.sequence": self.sequences[key]})
                if len(pieces) > 1:
                    attributes.update({"log.segment.index": index, "log.segment.count": len(pieces),
                        "log.framing": framing + "_segment"})
                self.emit(dict(value, body=piece, attributes=attributes))
        self.observed[key] = datetime.now(timezone.utc).isoformat()

    def _drain(self, key, force=False):
        while self.pending.get(key):
            text = self.pending[key]
            if key in self.delimiters:
                if text == "\r" and not force:
                    return
                self.delimiters.remove(key)
                if text.startswith("\r\n"):
                    text = text[2:]
                elif text.startswith("\n"):
                    text = text[1:]
                self.pending[key] = text
                if not text:
                    return
            if text.lstrip().startswith(("{", "[")):
                try:
                    offset = len(text) - len(text.lstrip())
                    _, end = json.JSONDecoder().raw_decode(text, offset)
                    # Retain whitespace in the formatted document, excluding only
                    # its record delimiter. Chunks may split UTF-8 and JSON tokens.
                    self._send(key, text[:end], "json_document")
                    self.pending[key] = text[end:]
                    self.delimiters.add(key)
                    continue
                except ValueError:
                    if not force:
                        if len(text) > MAX_PENDING:
                            # Keep a full 64 KiB overlap (larger than supported
                            # credentials) while bounding malformed/unfinished
                            # output. Redaction runs before the prefix is split.
                            safe = redact(text)
                            self._send(key, safe[:-MAX_FRAME], "bounded_partial")
                            self.pending[key] = safe[-MAX_FRAME:]
                        return
            if "\n" in text:
                line, remaining = text.split("\n", 1)
                self._send(key, line, "newline")
                self.pending[key] = remaining
            elif force:
                self._send(key, text, "partial_line")
                self.pending[key] = ""
            else:
                if len(text) > MAX_PENDING:
                    safe = redact(text)
                    self._send(key, safe[:-MAX_FRAME], "bounded_partial")
                    self.pending[key] = safe[-MAX_FRAME:]
                return

    def flush(self):
        for key in list(self.pending):
            self._drain(key, force=True)


class RingWriter:
    def __init__(self, directory=ROOT):
        self.path = Path(directory) / "process-output.jsonl"
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.last_warning = 0.0

    def __call__(self, value):
        try:
            if self.path.exists() and self.path.stat().st_size >= MAX_FILE:
                self.path.replace(self.path.with_suffix(".jsonl.1"))
            data = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                remaining = memoryview(data)
                while remaining:
                    count = os.write(fd, remaining)
                    if count == 0:
                        raise OSError("zero-length telemetry write")
                    remaining = remaining[count:]
            finally:
                os.close(fd)
        except OSError:
            if time.monotonic() - self.last_warning > 30:
                self.last_warning = time.monotonic()
                print("Telemetry output tee could not append; original console output is unaffected.", file=sys.stderr, flush=True)


def headers(line):
    if isinstance(line, bytes):
        line = line.decode("ascii")
    return dict(item.split(":", 1) for item in line.strip().split())


def main(directory=ROOT):
    from .telemetry import enabled
    framer = Framer(RingWriter(directory) if enabled() else lambda _record: None)
    def stop(_signum, _frame):
        framer.flush()
        # The filelog receiver polls every 200 ms. It stops after this listener.
        time.sleep(0.25)
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop)
    # Supervisor 4.x's envelope len is the Unicode payload length, matching its
    # official text-mode listener API—not the UTF-8 byte count. Preserve CRLF.
    source = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", newline="")
    target = sys.stdout.buffer
    while True:
        target.write(b"READY\n"); target.flush()
        line = source.readline()
        if not line:
            framer.flush()
            return
        envelope = headers(line)
        length = int(envelope["len"])
        payload = source.read(length)
        try:
            if envelope["eventname"].startswith("TICK_"):
                framer.flush()
            else:
                metadata, chunk = payload.split("\n", 1)
                fields = headers(metadata)
                if fields["processname"] in SERVICES:
                    # Supervisor represents a chunk ending mid-codepoint as a
                    # bytes literal. Recover those actual bytes for incremental
                    # UTF-8 decoding instead of forwarding its escape wrapper.
                    recovered = None
                    if chunk.startswith("Undecodable: b"):
                        try:
                            value = ast.literal_eval(chunk[len("Undecodable: "):])
                            if isinstance(value, bytes):
                                recovered = value
                        except (ValueError, SyntaxError):
                            pass
                    framer.feed(fields["processname"], fields["pid"], fields["channel"],
                                recovered if recovered is not None else chunk.encode("utf-8"))
        except Exception:
            # A malformed telemetry event must not stall Supervisor or child output.
            print("Telemetry output tee rejected an event frame.", file=sys.stderr, flush=True)
        target.write(b"RESULT 2\nOK"); target.flush()


if __name__ == "__main__":
    main()
