"""Send bounded, credential-gated incident summaries to a configured T3 task."""

import argparse
import asyncio
import hashlib
from itertools import chain
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from . import t3_webhook
from .incidents_auth import (
    clear_t3_auth_attention, codex_auth_attention, mark_codex_auth_attention,
    mark_t3_auth_attention, read_timestamp,
)
from .interpreter import CodexInterpreter, InterpretationError
from .status import AUTH_REQUIRED_STATES, project_execution_diagnostic


LOG = logging.getLogger(__name__)
POLL_SECONDS = 10
HEARTBEAT_AGE = 120
AUTH_EVIDENCE_AGE = 900
RECENT_ERROR_AGE = 600
PER_INCIDENT_COOLDOWN = 3600
GLOBAL_COOLDOWN = 300
TAIL_BYTES = 128 * 1024
AUTH_CODES = AUTH_REQUIRED_STATES | {
    "http_401", "http_403", "credential_missing", "credentials_missing", "jev_auth_required", "jev_credentials_missing",
}
EXPECTED_EXCEPTIONS = {"Hold", "RetryHold", "BrokerPreflightHold", "SourceContextChanged", "ImageTransportError"}
EXPECTED_CODES = {"invalid_evidence"}
OUTAGE_STATES = {"error", "unavailable", "failed", "disconnected", "degraded", "needs_attention"}
HEALTHY_STATES = {"ready", "connected", "paper"}
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "maxLength": 420},
        "next_check": {"type": "string", "maxLength": 240},
    },
    "required": ["summary", "next_check"],
    "additionalProperties": False,
}
SUMMARY_PROMPT = (
    "Summarize this relay failure for an engineer in plain English. Identify the observed failure, "
    "its stage, and a concrete read-only next check. Distinguish evidence from hypotheses. "
    "The input contains only diagnostic metadata. Do not invent facts, credentials, URLs, "
    "commands, or instructions to trade, deploy, or modify any system."
)
SAFE_DIAGNOSTIC_FIELDS = ("stage", "code", "exception", "broker_operation", "tool", "check", "schema_check", "schema_reason")
DIAGNOSTIC_PREFIX = b" Execution diagnostic "


def _read_json(path, limit=1024 * 1024):
    try:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        value = json.loads(data) if len(data) <= limit else None
    except (OSError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _status_path(config_path, settings):
    configured = settings.get("runtime_status_file")
    return config_path.parent / (configured if isinstance(configured, str) and configured else "state/runtime-status.json")


def _status(value, provider):
    section = value.get(provider)
    state = section.get("state") if isinstance(section, dict) else None
    return state if isinstance(state, str) else ""


def _auth_fault(snapshot, path, now):
    if codex_auth_attention(path, snapshot, now):
        return True
    for provider in ("discord", "codex", "broker", "jev"):
        section = snapshot.get(provider)
        if isinstance(section, dict) and (
            _status(snapshot, provider) in AUTH_CODES
            or section.get("auth_required") is True
            or section.get("reauth_required") is True
        ):
            return True
    return False


def _tail_lines(path):
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - TAIL_BYTES))
            if size > TAIL_BYTES:
                stream.readline(TAIL_BYTES)
            data = stream.read(TAIL_BYTES)
        return data[:data.rfind(b"\n") + 1].splitlines()
    except OSError:
        return ()


def _diagnostics(path, start, now):
    """Scan only the active bounded tail and one adjacent rotation."""
    for file in (path, path.with_suffix(path.suffix + ".1")):
        for line in reversed(_tail_lines(file)):
            _, separator, body = line.partition(DIAGNOSTIC_PREFIX)
            if not separator or len(body) > 32768:
                continue
            try:
                record = json.loads(body)
            except (ValueError, RecursionError):
                continue
            if not isinstance(record, dict) or record.get("event") != "failure":
                continue
            at = read_timestamp(record.get("at"))
            # Let provider status publication settle before routing a failure.
            if at is None or not max(start, now - RECENT_ERROR_AGE) <= at <= now - 5:
                continue
            diagnostic = project_execution_diagnostic(record.get("diagnostic"))
            details = [diagnostic, *diagnostic.get("causes", ())]
            if any(item.get("code") in AUTH_CODES for item in details):
                continue
            if diagnostic.get("exception") in EXPECTED_EXCEPTIONS or diagnostic.get("code") in EXPECTED_CODES:
                continue
            if not diagnostic.get("id") or not diagnostic.get("code"):
                continue
            facts = {"kind": "execution_failure", "diagnostic_id": diagnostic["id"], "observed_at": record["at"]}
            for key in SAFE_DIAGNOSTIC_FIELDS:
                value = diagnostic.get(key)
                if value:
                    facts[key] = value
            leaf = next((item for item in reversed(details) if item.get("frames")), None)
            if leaf:
                facts["source_location"] = leaf["frames"][-1]
            revision = diagnostic.get("build_revision")
            if revision:
                facts["source_revision"] = revision
            yield facts


def _fingerprint(facts):
    categories = ("kind", "component", "stage", "code", "exception", "tool", "broker_operation", "source_location", "source_revision")
    identity = json.dumps([facts.get(key) for key in categories], separators=(",", ":")).encode()
    return hashlib.sha256(identity).hexdigest()


class IncidentJournal:
    def __init__(self, file, now):
        file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self.db = sqlite3.connect(file)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (fingerprint TEXT PRIMARY KEY, at REAL NOT NULL);
        """)
        self.start = self._initialize(now)

    def _initialize(self, now):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('start', ?)", (now,))
        return self.value("start")

    def value(self, key):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else 0

    def update(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (key, value))

    def available(self, fingerprint, now):
        row = self.db.execute("SELECT at FROM attempts WHERE fingerprint=?", (fingerprint,)).fetchone()
        return (not row or now - row[0] >= PER_INCIDENT_COOLDOWN) and now - self.value("last_attempt") >= GLOBAL_COOLDOWN

    def claim(self, fingerprint, now):
        # Persist intent first. A crash or lost response can't start the same
        # remote work again on the next poll or restart.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if not self.available(fingerprint, now):
                return False
            self.db.execute("DELETE FROM attempts WHERE at < ?", (now - PER_INCIDENT_COOLDOWN,))
            self.db.execute("INSERT OR REPLACE INTO attempts VALUES (?, ?)", (fingerprint, now))
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('last_attempt', ?)", (now,))
            return True


def _validate_summary(value):
    if not isinstance(value, dict) or set(value) != set(SUMMARY_SCHEMA["required"]):
        raise ValueError("invalid incident summary")
    for key, field in SUMMARY_SCHEMA["properties"].items():
        text = value[key]
        if (
            not isinstance(text, str) or not text.strip() or len(text) > field["maxLength"]
            or not re.fullmatch(r"[A-Za-z0-9 .,;:()'/_-]+", text)
            or "://" in text
            or re.search(r"[A-Za-z0-9_-]{28,}", text)
        ):
            raise ValueError("invalid incident summary")
    return value


def summarize(settings, facts):
    llm = settings.get("llm")
    llm = llm if isinstance(llm, dict) else {}
    interpreter = CodexInterpreter({"llm": {key: llm[key] for key in ("executable",) if key in llm} | {
        "timeout_seconds": 18,
        "reasoning_effort": "low",
    }})
    return interpreter.summarize_incident(facts, SUMMARY_SCHEMA, SUMMARY_PROMPT)


def _fallback(facts):
    if facts["kind"] == "runtime_outage":
        return {
            "summary": f"The relay reports {facts['component']} as {facts['code']}.",
            "next_check": "Inspect the latest service status and the first failing operation.",
        }
    component = facts.get("tool") or facts.get("broker_operation")
    operation = f" while calling {component}" if component else ""
    return {
        "summary": f"The relay recorded {facts['code']} during {facts.get('stage', 'execution')}{operation}.",
        "next_check": "Inspect the reported source location and compare the failing operation with the deployed revision.",
    }


class IncidentReporter:
    def __init__(self, config_path, endpoint, *, clock=time.time, summarize_report=summarize, send=t3_webhook.deliver):
        self.config_path = Path(config_path).resolve()
        self.log = self.config_path.parent / "state/execution-diagnostics.log"
        self.endpoint = endpoint
        self.clock = clock
        self.summarize = summarize_report
        self.send = send
        self.journal = IncidentJournal(self.log.parent / "incidents.sqlite3", clock())
        self.last_outage = None
        self.outage_since = 0

    def _outage(self, runtime, fresh, now):
        problem = next(((provider, _status(runtime, provider))
                        for provider in ("discord", "broker", "codex", "jev")
                        if _status(runtime, provider) in OUTAGE_STATES), None)
        if not fresh:
            problem = ("worker", "heartbeat_stale")
        elif runtime.get("state") == "error":
            problem = ("worker", "error")
        if self.last_outage != problem:
            self.last_outage, self.outage_since = problem, now
        if problem is None or now - self.outage_since < 30:
            return None
        facts = {"kind": "runtime_outage", "component": problem[0], "code": problem[1],
                 "observed_at": datetime.fromtimestamp(self.outage_since, timezone.utc).isoformat()}
        revision = os.environ.get("RELAY_SOURCE_REVISION", "")
        if re.fullmatch(r"[0-9a-f]{40}", revision):
            facts["source_revision"] = revision
        return facts

    async def poll(self):
        now = self.clock()
        settings = _read_json(self.config_path)
        runtime_path = _status_path(self.config_path, settings)
        runtime = _read_json(runtime_path)
        if _auth_fault(runtime, runtime_path, now):
            self.journal.update("last_auth_good", 0)
            self.last_outage = None
            return
        heartbeat = read_timestamp(runtime.get("heartbeat_at"))
        fresh = heartbeat is not None and -5 <= now - heartbeat <= HEARTBEAT_AGE
        if fresh and all(_status(runtime, name) in HEALTHY_STATES for name in ("discord", "broker", "codex")):
            self.journal.update("last_auth_good", heartbeat)
        auth_evidence = self.journal.value("last_auth_good")
        if not auth_evidence or not -5 <= now - auth_evidence <= AUTH_EVIDENCE_AGE:
            return
        outage = self._outage(runtime, fresh, now)
        for facts in chain(_diagnostics(self.log, self.journal.start, now), (outage,) if outage else ()):
            fingerprint = _fingerprint(facts)
            if self.journal.available(fingerprint, now):
                await self._report(settings, runtime_path, facts, fingerprint)
                return

    async def _report(self, settings, runtime_path, facts, fingerprint):
        summary_source = "codex"
        try:
            summary = await asyncio.to_thread(self.summarize, settings, facts)
            summary = _validate_summary(summary)
        except Exception as exc:
            if isinstance(exc, InterpretationError) and exc.code in AUTH_CODES:
                mark_codex_auth_attention(runtime_path)
                self.journal.update("last_auth_good", 0)
                return
            summary, summary_source = _fallback(facts), "diagnostic_fallback"
        now = self.clock()
        if _auth_fault(_read_json(runtime_path), runtime_path, now) or not self.journal.claim(fingerprint, now):
            return
        incident_id = facts.get("diagnostic_id") or hashlib.sha256(
            f"{fingerprint}:{facts['observed_at']}".encode()
        ).hexdigest()
        payload = {
            "event": "relay.incident", "schema_version": 1, "application": "discord-option-tailer",
            "incident_id": incident_id, "fingerprint": fingerprint, "summary_source": summary_source,
            **summary, "diagnostic": facts,
        }
        try:
            await asyncio.to_thread(self.send, self.endpoint, payload)
            clear_t3_auth_attention(runtime_path)
            LOG.info("T3 incident report delivered (incident=%s)", incident_id)
        except t3_webhook.DeliveryError as exc:
            LOG.warning("T3 incident report failed (incident=%s, code=%s, http_status=%s)",
                        incident_id, exc.code, exc.status or "unavailable")
            if exc.cf_ray:
                LOG.warning("T3 webhook Cloudflare request (incident=%s, cf_ray=%s)", incident_id, exc.cf_ray)
            if exc.status in {401, 403}:
                mark_t3_auth_attention(runtime_path)


async def serve(config_path):
    try:
        endpoint = t3_webhook.load_config()
    except ValueError:
        LOG.error("T3 incident reporting requires a valid webhook URL and signing secret in the environment")
        return
    if endpoint is None:
        return
    reporter = IncidentReporter(config_path, endpoint)
    try:
        while True:
            try:
                await reporter.poll()
            except Exception as exc:
                LOG.warning("Incident reporting poll failed (%s)", type(exc).__name__)
            await asyncio.sleep(POLL_SECONDS)
    finally:
        reporter.journal.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.local.json")
    options = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(serve(options.config))


if __name__ == "__main__":
    main()
