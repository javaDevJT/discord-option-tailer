"""Loopback monitoring API with opt-in, authenticated-gateway setup controls."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import tempfile
from .account import AUTH_ERROR, CACHE_KEY, REFRESH_ERROR, REFRESH_SECONDS
from .status import project_execution_diagnostic
from .images import collect_images, download_images, ImageTransportError, MAX_IMAGES
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlencode, urlsplit


UTC = timezone.utc
MAX_BODY = 4 * 1024 * 1024
MAX_QUERY_TEXT = 200
MAX_OFFSET = 100_000
RUNTIME_STALE_SECONDS = 30
IMAGE_PREVIEW_SLOTS = threading.BoundedSemaphore(2)
MESSAGE_STATES = frozenset({
    "observed", "evaluating", "evaluated", "context", "ignore", "wait", "open", "reduce", "close", "update_stop",
    "shadow_order", "paper_order", "broker_order", "held", "unknown", "error", "duplicate",
    "invalid", "untrusted", "recovery_pending", "recovery_evaluating", "recovery_review",
    "recovery_error",
})
RECOVERY_STATUSES = frozenset({"viable", "invalidated", "uncertain", "not_actionable"})
ORDER_STATUSES = frozenset({
    "submitting", "unknown", "rejected", "filled", "canceled", "expired", "submitted", "open",
    "partially_filled", "pending",
})
EVENT_STATES = MESSAGE_STATES | ORDER_STATUSES
DECISION_FIELDS = (
    "action", "origin_message_id", "contract", "quantity", "fraction", "alert_price", "stop_price",
    "confidence", "ambiguous", "reason", "evidence", "order_proposal", "recovery", "entry_evaluation",
    "evaluation_timing", "profit_only", "exit_evaluation", "stop_evaluation", "order_reconciliation",
    "execution_diagnostic",
)
SIZING_FIELDS = (
    "method", "source_group", "equity", "parse_confidence", "risk_fraction", "confidence_cap_fraction",
    "budget", "binding_limit", "premium_risk_per_contract", "allocated_premium_risk",
    "minimum_contract_fallback", "available_buying_power",
)
ORDER_BODY_FIELDS = (
    "side", "quantity", "limit_price", "position_effect", "quote_timestamp", "account_timestamp", "sizing",
)

STOP_STATUSES = frozenset({"shadow", "pending", "active", "complete", "blocked"})
STOP_ORDER_TYPES = frozenset({"stop_market", "market"})


class QueryError(ValueError):
    pass


class ConfigUnavailable(RuntimeError):
    pass


def _safe_text(value, limit=4000):
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "")[:limit]


def _safe_state(value):
    value = _safe_text(value, 64)
    return value if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", value) else "unknown"


def _safe_identifier(value, limit=128):
    value = _safe_text(value, limit)
    return value if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) else ""


def _safe_channel_id(value):
    value = _safe_text(value, 64)
    return value if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", value) else ""


def _json_object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_list(value):
    if isinstance(value, list):
        return value
    return []


def _safe_scalar(value, limit=4000):
    if isinstance(value, str):
        return _safe_text(value, limit)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    return None


def _project_contract(value):
    contract = _json_object(value)
    result = {}
    for key in ("symbol", "expiry", "strike", "option_type"):
        if key in contract:
            scalar = _safe_scalar(contract[key], 64)
            if scalar is not None:
                result[key] = scalar
    return result


def _account_money(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool) or len(str(value)) > 100:
        return None
    try:
        return str(value) if Decimal(str(value)).is_finite() else None
    except InvalidOperation:
        return None


def _positive_money(value):
    rendered = _account_money(value)
    if rendered is None:
        return None
    try:
        return rendered if Decimal(rendered) > 0 else None
    except InvalidOperation:
        return None


def _project_stop_evaluation(value):
    evaluation = _json_object(value)
    if not evaluation:
        return None
    result = {}
    status = _safe_text(evaluation.get("status"), 64).lower()
    if status in STOP_STATUSES:
        result["status"] = status
    elif "status" in evaluation:
        result["status"] = "unknown"
    stop_price = _positive_money(evaluation.get("stop_price"))
    if stop_price is not None:
        result["stop_price"] = stop_price
    reason = _safe_text(evaluation.get("reason"), 4000)
    if reason:
        result["reason"] = reason
    order_id = _safe_identifier(evaluation.get("order_id"), 256)
    if order_id:
        result["order_id"] = order_id
    if result and "status" not in result:
        result["status"] = "unknown"
    return result or None


def _project_order_reconciliation(value):
    reconciliation = _json_object(value)
    if not reconciliation:
        return None
    result = {}
    if "status" in reconciliation:
        status = _safe_text(reconciliation["status"], 64).lower()
        if status in ORDER_STATUSES:
            result["status"] = status
    for key in ("filled_quantity", "requested_quantity"):
        quantity = reconciliation.get(key)
        if type(quantity) is int and 0 <= quantity <= 1_000_000:
            result[key] = quantity
    broker_id = _safe_broker_id(reconciliation.get("broker_order_id"))
    if broker_id:
        result["broker_order_id"] = broker_id
    if "fill_price" in reconciliation:
        fill_price = _safe_scalar(reconciliation["fill_price"], 256)
        if fill_price is not None:
            result["fill_price"] = fill_price
    return result or None


def _project_order_protection(value):
    body = _json_object(value)
    result = {}
    order_type = _safe_text(body.get("order_type"), 32).lower()
    if order_type in STOP_ORDER_TYPES:
        result["order_type"] = order_type
    stop_price = _positive_money(body.get("stop_price"))
    if stop_price is not None:
        result["stop_price"] = stop_price
    requested_stop_price = _positive_money(body.get("requested_stop_price"))
    if requested_stop_price is not None:
        result["requested_stop_price"] = requested_stop_price
    if order_type == "market" and not stop_price and not requested_stop_price:
        return None
    return result or None


def _project_embeds(value):
    """Keep display text and metadata; attachment URLs and arbitrary fields stay private."""
    result = []
    for embed in _json_list(value)[:50]:
        if not isinstance(embed, dict):
            continue
        item = {}
        for key in ("title", "description", "text", "type", "timestamp", "color"):
            if key in embed:
                scalar = _safe_scalar(embed[key], 8000)
                if scalar is not None:
                    item[key] = scalar
        for parent, key in (("author", "name"), ("footer", "text")):
            nested = embed.get(parent)
            if isinstance(nested, dict) and isinstance(nested.get(key), str):
                item[parent] = {key: _safe_text(nested[key], 1000)}
        fields = []
        for field in _json_list(embed.get("fields"))[:50]:
            if not isinstance(field, dict):
                continue
            field_item = {}
            for key in ("name", "value"):
                if isinstance(field.get(key), str):
                    field_item[key] = _safe_text(field[key], 4000)
            if type(field.get("inline")) is bool:
                field_item["inline"] = field["inline"]
            if field_item:
                fields.append(field_item)
        if fields:
            item["fields"] = fields
        result.append(item)
    return result


def _project_sizing(value):
    sizing = _json_object(value)
    return {key: _safe_scalar(sizing[key], 256) for key in SIZING_FIELDS if key in sizing and _safe_scalar(sizing[key], 256) is not None}


def _project_entry_evaluation(value):
    evaluation = _json_object(value)
    return {key: number for key in (
        "ask", "reference_price", "ask_deviation_percent", "limit_price",
        "limit_deviation_percent", "max_chase_percent",
    ) if (number := _account_money(evaluation.get(key))) is not None}


def _project_order_proposal(value):
    order = _json_object(value)
    result = {}
    if "entry_evaluation" in order:
        result["entry_evaluation"] = _project_entry_evaluation(order["entry_evaluation"])
    for key in ("client_order_id", "side", "quantity", "limit_price", "position_effect", "quote_timestamp", "account_timestamp", "signal_fingerprint"):
        if key not in order:
            continue
        if key == "client_order_id":
            result[key] = _safe_identifier(order[key], 128)
        elif key == "signal_fingerprint":
            result[key] = _safe_identifier(order[key], 128)
        elif key == "quantity":
            if type(order[key]) is int and 0 <= order[key] <= 1_000_000:
                result[key] = order[key]
        else:
            scalar = _safe_scalar(order[key], 256)
            if scalar is not None:
                result[key] = scalar
    if "contract" in order:
        result["contract"] = _project_contract(order["contract"])
    if "sizing" in order:
        result["sizing"] = _project_sizing(order["sizing"])
    return result


def _project_evidence(value):
    evidence = []
    for entry in _json_list(value)[:20]:
        if not isinstance(entry, dict):
            continue
        item = {}
        if "message_id" in entry:
            item["message_id"] = _safe_identifier(entry["message_id"], 128)
        if "quote" in entry:
            item["quote"] = _safe_text(entry["quote"], 6000)
        if item:
            evidence.append(item)
    return evidence


def _safe_timestamp(value):
    timestamp = _safe_text(value, 128)
    return timestamp if _parse_iso(timestamp) is not None else None


def _project_recovery_quote(value):
    quote = _json_object(value)
    result = {}
    if "contract" in quote:
        result["contract"] = _project_contract(quote["contract"]) if quote["contract"] is not None else None
    for key in ("bid", "ask", "multiplier", "currency", "asset_type", "tick_size"):
        if key in quote:
            scalar = _safe_scalar(quote[key], 256)
            if scalar is not None:
                result[key] = scalar
    if "timestamp" in quote:
        timestamp = _safe_timestamp(quote["timestamp"])
        if timestamp is not None:
            result["timestamp"] = timestamp
    if type(quote.get("tradable")) is bool:
        result["tradable"] = quote["tradable"]
    return result


def _project_recovery_facts(value):
    facts = _json_object(value)
    result = {}
    for key in ("evaluated_at", "original_timestamp", "snapshot_timestamp"):
        if key in facts:
            timestamp = _safe_timestamp(facts[key])
            if timestamp is not None:
                result[key] = timestamp
    age = facts.get("signal_age_seconds")
    if type(age) in (int, float) and age == age and 0 <= age <= 31_536_000:
        result["signal_age_seconds"] = age
    for key in ("equity", "buying_power"):
        if key in facts:
            scalar = _safe_scalar(facts[key], 256)
            if scalar is not None:
                result[key] = scalar
    if "affordable_quantity" in facts:
        quantity = facts["affordable_quantity"]
        if quantity is None or (type(quantity) is int and 0 <= quantity <= 1_000_000):
            result["affordable_quantity"] = quantity
    for key in ("context_truncated", "context_changed"):
        if type(facts.get(key)) is bool:
            result[key] = facts[key]
    market_open = facts.get("market_open")
    if market_open is None or type(market_open) is bool:
        if "market_open" in facts:
            result["market_open"] = market_open
    if "quote" in facts:
        result["quote"] = _project_recovery_quote(facts["quote"]) if facts["quote"] is not None else None
    if "blockers" in facts:
        result["blockers"] = [
            _safe_text(item, 1000) for item in _json_list(facts["blockers"])[:20] if isinstance(item, str)
        ]
    return result


def _project_evaluation_timing(value):
    timing = _json_object(value)
    result = {}
    for key in (
        "model_duration_seconds", "rules_duration_seconds", "posted_to_receipt_seconds", "posted_to_interpretation_seconds",
        "posted_to_decision_seconds", "jev_duration_seconds",
        "codex_duration_seconds", "queue_wait_seconds", "preparation_seconds",
        "interpretation_seconds", "execution_wait_seconds", "execution_seconds",
        "snapshot_seconds", "quote_seconds", "source_verification_seconds",
        "contract_resolution_seconds", "submission_seconds", "received_to_submission_seconds",
        "posted_to_submission_seconds",
        "broker_ack_seconds", "fill_seconds",
    ):
        number = timing.get(key)
        if type(number) in (int, float) and not isinstance(number, bool) and math.isfinite(number) and 0 <= number <= 315_360_000:
            result[key] = number
    for key in (
        "semantic_confidence", "allocation_confidence", "selected_probability",
        "native_confidence", "eligibility_probability",
    ):
        number = timing.get(key)
        if type(number) in (int, float) and not isinstance(number, bool) and math.isfinite(number) and 0 <= number <= 1:
            result[key] = number
    attempts = timing.get("attempts")
    if type(attempts) is int and 1 <= attempts <= 2:
        result["attempts"] = attempts
    for key in ("decision_at", "received_at", "interpretation_ready_at", "broker_result_at"):
        timestamp = _safe_timestamp(timing.get(key))
        if timestamp is not None:
            result[key] = timestamp
    path = _safe_text(timing.get("path"), 32)
    if path in {"direct", "recovery", "historical"}:
        result["path"] = path
    for key in ("evaluator", "provider"):
        provider = _safe_text(timing.get(key), 32).lower()
        if provider in {"rules", "jev", "codex", "synthetic"}:
            result[key] = provider
    route = _safe_text(timing.get("route"), 32).lower()
    if route in {"direct", "jev", "codex", "fallback", "shadow"}:
        result["route"] = route
    fallback_reason = _safe_identifier(timing.get("fallback_reason"), 64).lower()
    if fallback_reason and re.fullmatch(r"[a-z0-9_.:-]{1,64}", fallback_reason):
        result["fallback_reason"] = fallback_reason
    model = _safe_identifier(timing.get("model"), 128)
    if model:
        result["model"] = model
    jev_model = _safe_identifier(timing.get("jev_model"), 128)
    if jev_model:
        result["jev_model"] = jev_model
    if isinstance(timing.get("jev_shadow_action"), str) and timing["jev_shadow_action"] in {"OPEN", "REDUCE", "CLOSE", "WAIT", "IGNORE", "UPDATE_STOP"}:
        result["jev_shadow_action"] = timing["jev_shadow_action"]
    if type(timing.get("jev_shadow_agrees")) is bool:
        result["jev_shadow_agrees"] = timing["jev_shadow_agrees"]
    broker_result_status = _safe_identifier(timing.get("broker_result_status"), 32).lower()
    if broker_result_status in {
        "accepted", "rejected", "unknown", "filled", "submitted", "canceled", "error",
        "open", "partially_filled", "pending", "expired",
    }:
        result["broker_result_status"] = broker_result_status
    for key in ("eligible", "fast_path", "timed_out", "clock_uncertain"):
        if type(timing.get(key)) is bool:
            result[key] = timing[key]
    if type(timing.get("delayed")) is bool:
        result["delayed"] = timing["delayed"]
    return result


def _project_recovery(value):
    recovery = _json_object(value)
    if not recovery:
        return None
    result = {}
    if recovery.get("execution") == "exit":
        result["execution"] = "exit"
    status = _safe_text(recovery.get("status"), 64)
    if status in RECOVERY_STATUSES:
        result["status"] = status
    if "confidence" in recovery:
        confidence = recovery["confidence"]
        if confidence is None or (type(confidence) in (int, float) and confidence == confidence and abs(confidence) <= 1_000_000):
            result["confidence"] = confidence
    if "reason" in recovery:
        result["reason"] = _safe_text(recovery["reason"], 4000)
    if "evidence" in recovery:
        result["evidence"] = _project_evidence(recovery["evidence"])
    for key in ("evaluated_at", "original_timestamp"):
        if key in recovery:
            timestamp = _safe_timestamp(recovery[key])
            if timestamp is not None:
                result[key] = timestamp
    age = recovery.get("signal_age_seconds")
    if type(age) in (int, float) and age == age and 0 <= age <= 31_536_000:
        result["signal_age_seconds"] = age
    if "facts" in recovery:
        result["facts"] = _project_recovery_facts(recovery["facts"])
    if "evaluation_timing" in recovery:
        result["evaluation_timing"] = _project_evaluation_timing(recovery["evaluation_timing"])
    return result


def _project_decision(value):
    decision = _json_object(value)
    if not decision:
        return None
    result = {}
    for key in DECISION_FIELDS:
        if key not in decision:
            continue
        current = decision[key]
        if key == "contract":
            result[key] = _project_contract(current) if current is not None else None
        elif key == "evidence":
            result[key] = _project_evidence(current)
        elif key == "order_proposal":
            result[key] = _project_order_proposal(current)
        elif key == "recovery":
            result[key] = _project_recovery(current)
        elif key == "entry_evaluation":
            result[key] = _project_entry_evaluation(current)
        elif key == "execution_diagnostic":
            result[key] = project_execution_diagnostic(current)
        elif key == "evaluation_timing":
            result[key] = _project_evaluation_timing(current)
        elif key == "stop_evaluation":
            projected = _project_stop_evaluation(current)
            if projected is not None:
                result[key] = projected
        elif key == "order_reconciliation":
            projected = _project_order_reconciliation(current)
            if projected is not None:
                result[key] = projected
        elif key == "exit_evaluation":
            result[key] = {name: _safe_scalar(value, 128) for name, value in _json_object(current).items()
                           if name in {"owned_quantity", "sell_quantity", "requested_fraction", "default_half", "profit_only",
                                       "evaluated_sell_price", "average_entry_price", "round_trip_fee_reserve_per_contract", "estimated_net_profit"}}
        elif key in {"action", "origin_message_id", "reason"}:
            result[key] = _safe_text(current, 4000 if key == "reason" else 128)
        elif key in {"quantity"}:
            if current is None or (type(current) is int and 0 <= current <= 1_000_000):
                result[key] = current
        elif key in {"fraction", "confidence"}:
            if current is None or (type(current) in (int, float) and current == current and abs(current) <= 1_000_000):
                result[key] = current
        elif key in {"ambiguous", "profit_only"}:
            if type(current) is bool:
                result[key] = current
        else:
            result[key] = _safe_scalar(current, 256)
    return result


def _project_event_state(value, decision):
    state = _safe_state(value)
    reconciliation = decision.get("order_reconciliation") if isinstance(decision, dict) else None
    reconciled_status = reconciliation.get("status") if isinstance(reconciliation, dict) else None
    if state == "unknown" and reconciled_status in ORDER_STATUSES:
        return reconciled_status
    return state


def _project_message(row, event):
    body = _json_object(row["body"])
    author = body.get("author") if isinstance(body.get("author"), dict) else {}
    author_id = body.get("author_id") or author.get("id") or ""
    author_name = body.get("author_name") or author.get("global_name") or author.get("nickname") or author.get("name") or author.get("username") or ""
    latest_event = None
    if event is not None and event["id"] is not None:
        decision = _project_decision(event["decision"])
        latest_event = {
            "state": _project_event_state(event["state"], decision),
            "reason": _safe_text(event["reason"], 4000),
            "decision": decision,
            "created_at": _safe_text(event["created_at"], 128),
        }
    return {
        "id": _safe_identifier(row["id"]),
        "channel_id": _safe_channel_id(row["channel_id"]),
        "source_group": _safe_identifier(row["source_group"]),
        "timestamp": _safe_text(row["timestamp"], 128),
        "revision": _safe_identifier(row["revision"], 128),
        "author": {"id": _safe_identifier(author_id), "name": _safe_text(author_name, 256)},
        "content": _safe_text(body.get("content", ""), 20000),
        "embeds": _project_embeds(body.get("embeds")),
        "images": _project_message_images(row, body),
        "latest_event": latest_event,
    }


def _project_message_images(row, body):
    try:
        sources = collect_images([dict(body, id=str(row["id"]))])
    except ImageTransportError:
        return []
    return [{"index": index, "url": "/api/message-image?" + urlencode({
        "message_id": row["id"], "revision": row["revision"], "index": index,
    })} for index in range(len(sources))]


def _project_event(row):
    decision = _project_decision(row["decision"])
    return {
        "id": row["id"] if type(row["id"]) is int else 0,
        "message_id": _safe_identifier(row["message_id"]),
        "revision": _safe_identifier(row["revision"], 128),
        "state": _project_event_state(row["state"], decision),
        "reason": _safe_text(row["reason"], 4000),
        "decision": decision,
        "created_at": _safe_text(row["created_at"], 128),
    }


def _project_order(row, mode):
    body = _json_object(row["body"])
    contract = _project_contract(row["contract"] or body.get("contract"))
    result = {
        "id": _safe_identifier(row["id"]),
        "contract": contract,
        "action": _safe_state(row["action"]),
        "side": _safe_state(body.get("side")),
        "quantity": body.get("quantity") if type(body.get("quantity")) is int and 0 <= body["quantity"] <= 1_000_000 else 0,
        "limit_price": _safe_scalar(body.get("limit_price"), 256),
        "position_effect": _safe_state(body.get("position_effect")),
        "status": _safe_state(row["status"]),
        "filled_quantity": row["filled_quantity"] if type(row["filled_quantity"]) is int and row["filled_quantity"] >= 0 else 0,
        "filled_notional": _safe_scalar(row["filled_notional"], 256),
        "broker_id": _safe_broker_id(row["broker_id"]),
        "message_id": _safe_identifier(row["message_id"]),
        "created_at": _safe_text(row["created_at"], 128),
        "quote_timestamp": _safe_text(body.get("quote_timestamp"), 128),
        "account_timestamp": _safe_text(body.get("account_timestamp"), 128),
        "mode": mode,
        "sizing": _project_sizing(body.get("sizing")),
        "entry_evaluation": _project_entry_evaluation(body.get("entry_evaluation")),
    }
    protection = _project_order_protection(body)
    if protection:
        result["protection"] = protection
        for key in ("order_type", "stop_price", "requested_stop_price"):
            if key in protection:
                result[key] = protection[key]
    stop_evaluation = _project_stop_evaluation(body.get("stop_evaluation"))
    if stop_evaluation:
        result["stop_evaluation"] = stop_evaluation
    return result


def _safe_broker_id(value):
    value = _safe_identifier(value, 256)
    return None if not value or re.fullmatch(r"\d{5,20}", value) else value


def _project_position(row):
    result = {
        "source_group": _safe_identifier(row["source_group"]),
        "contract": _project_contract(row["contract"]),
        "quantity": row["quantity"] if type(row["quantity"]) is int and row["quantity"] > 0 else 0,
        "average_price": _safe_scalar(row["average_price"], 256),
    }
    keys = set(row.keys()) if hasattr(row, "keys") else set(row)
    if "stop_status" in keys:
        stop_evaluation = _project_stop_evaluation({
            "status": row["stop_status"],
            "reason": row["stop_reason"],
            "stop_price": row["stop_price"],
            "order_id": row["stop_order_id"],
        })
        if stop_evaluation:
            result["stop_evaluation"] = stop_evaluation
    return result


def _parse_iso(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _safe_detail(value):
    detail = " ".join(_safe_text(value, 400).split())
    if not detail:
        return ""
    lowered = detail.lower()
    credential_pattern = re.compile(
        r"(?:authorization\s*(?:header|token|credential|[:=])|"
        r"(?:bearer|basic)\s+[a-z0-9._~+/=-]{8,}|"
        r"(?:access[_ -]?token|refresh[_ -]?token|api[_ -]?key|secret|token)\s*[:=]\s*\S+)",
        re.IGNORECASE,
    )
    if any(term in lowered for term in ("traceback", "password", "account_number")) or credential_pattern.search(detail):
        return "details withheld"
    detail = re.sub(r"\b\d{5,20}\b", "[redacted]", detail)
    return detail


def _safe_runtime_section(value):
    section = value if isinstance(value, dict) else {}
    result = {"state": _safe_state(section.get("state"))}
    if "detail" in section:
        result["detail"] = _safe_detail(section.get("detail"))
    return result


def _safe_evaluation_runtime_section(value):
    result = _safe_runtime_section(value)
    section = value if isinstance(value, dict) else {}
    for key in ("evaluator", "provider"):
        provider = _safe_text(section.get(key), 32).lower()
        if provider in {"rules", "jev", "codex", "synthetic"}:
            result[key] = provider
    route = _safe_text(section.get("route"), 32).lower()
    if route in {"direct", "jev", "codex", "fallback", "shadow"}:
        result["route"] = route
    fallback_reason = _safe_identifier(section.get("fallback_reason"), 64).lower()
    if fallback_reason and re.fullmatch(r"[a-z0-9_.:-]{1,64}", fallback_reason):
        result["fallback_reason"] = fallback_reason
    model = _safe_identifier(section.get("model"), 128)
    if model:
        result["model"] = model
    for key in ("received_at", "interpretation_ready_at"):
        timestamp = _safe_timestamp(section.get(key))
        if timestamp is not None:
            result[key] = timestamp
    return result


def _safe_runtime(raw, path):
    unavailable = {
        "available": False,
        "stale": True,
        "state": "unavailable",
        "detail": "runtime status unavailable",
        "discord": {"state": "unknown", "channels": []},
        "codex": {"state": "unknown"},
        "broker": {"state": "unknown"},
        "evaluation": {"state": "unknown"},
    }
    if path is None or not path.is_file():
        return unavailable
    try:
        if path.stat().st_size > 1024 * 1024:
            return unavailable | {"detail": "runtime status unavailable"}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return unavailable | {"detail": "runtime status unavailable"}
    if not isinstance(value, dict):
        return unavailable
    updated_at = _safe_text(value.get("updated_at"), 128)
    parsed = _parse_iso(updated_at)
    age = (datetime.now(UTC) - parsed).total_seconds() if parsed is not None else None
    stale = parsed is None or age > RUNTIME_STALE_SECONDS or age < -5
    discord = value.get("discord") if isinstance(value.get("discord"), dict) else {}
    channels = []
    raw_channels = discord.get("channels")
    if isinstance(raw_channels, dict):
        raw_channels = [dict(value, id=key) for key, value in raw_channels.items() if isinstance(value, dict)]
    for channel in _json_list(raw_channels)[:50]:
        if not isinstance(channel, dict):
            continue
        item = {"id": _safe_channel_id(channel.get("id")), "state": _safe_state(channel.get("state"))}
        if "detail" in channel:
            item["detail"] = _safe_detail(channel.get("detail"))
        for key in ("updated_at", "last_seen_at"):
            timestamp = _safe_text(channel.get(key), 128)
            if _parse_iso(timestamp) is not None:
                item[key] = timestamp
        channels.append(item)
    result = {
        "available": True,
        "stale": stale,
        "updated_at": updated_at,
        "heartbeat_at": _safe_text(value.get("heartbeat_at"), 128),
        "state": _safe_state(value.get("state")),
        "detail": _safe_detail(value.get("detail")),
        "discord": {"state": _safe_state(discord.get("state")), "detail": _safe_detail(discord.get("detail")), "channels": channels},
        "codex": _safe_runtime_section(value.get("codex")),
        "broker": _safe_runtime_section(value.get("broker")),
        "jev": _safe_runtime_section(value.get("jev")),
        "evaluation": _safe_evaluation_runtime_section(value.get("evaluation")),
    }
    return result


EVALUATION_METRIC_DURATION_FIELDS = (
    "model_duration_seconds", "rules_duration_seconds", "posted_to_receipt_seconds", "posted_to_interpretation_seconds",
    "posted_to_decision_seconds", "queue_wait_seconds",
    "preparation_seconds", "jev_duration_seconds", "codex_duration_seconds",
    "interpretation_seconds", "execution_wait_seconds", "execution_seconds",
    "snapshot_seconds", "quote_seconds", "source_verification_seconds",
    "contract_resolution_seconds", "submission_seconds", "received_to_submission_seconds",
    "posted_to_submission_seconds",
    "broker_ack_seconds", "fill_seconds",
)


def _metric_percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]


def _empty_evaluation_metric_bucket():
    return {
        "total": 0,
        "evaluated": 0,
        "eligible": 0,
        "fast_path": 0,
        "direct": 0,
        "jev": 0,
        "fallback": 0,
        "failures": 0,
        "timeouts": 0,
        "routes": {},
        "fallback_reasons": {},
        "under_2s": {"count": 0, "denominator": 0, "rate": None},
        "coverage": {"fast_path": None, "eligible": None, "direct": None, "jev": None},
        "timings": {key: {"count": 0, "p50": None, "p95": None, "p99": None}
                    for key in EVALUATION_METRIC_DURATION_FIELDS},
    }


def _aggregate_evaluation_samples(samples):
    buckets = {name: _empty_evaluation_metric_bucket()
               for name in ("live", "historical", "recovery")}
    for sample in samples:
        bucket = buckets[sample["bucket"]]
        bucket["total"] += 1
        bucket["evaluated"] += int(sample["evaluated"])
        timing = sample["timing"]
        if timing.get("eligible") is True:
            bucket["eligible"] += 1
        fast_path = timing.get("fast_path") is True or timing.get("route") == "jev"
        if fast_path:
            bucket["fast_path"] += 1
        route = timing.get("route") or timing.get("evaluator") or "unknown"
        if route == "direct" or timing.get("evaluator") == "rules":
            bucket["direct"] += 1
        if route == "jev" or timing.get("evaluator") == "jev":
            bucket["jev"] += 1
        bucket["routes"][route] = bucket["routes"].get(route, 0) + 1
        reason = timing.get("fallback_reason")
        is_fallback = route == "fallback" or bool(reason)
        if is_fallback:
            bucket["fallback"] += 1
            if reason:
                bucket["fallback_reasons"][reason] = bucket["fallback_reasons"].get(reason, 0) + 1
        if sample["failed"]:
            bucket["failures"] += 1
        if sample["timed_out"]:
            bucket["timeouts"] += 1
        for key in EVALUATION_METRIC_DURATION_FIELDS:
            value = timing.get(key)
            if type(value) in (int, float) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                bucket["timings"][key].setdefault("_values", []).append(value)
        final_seconds = timing.get("posted_to_decision_seconds")
        if type(final_seconds) in (int, float) and not isinstance(final_seconds, bool) and math.isfinite(final_seconds) and final_seconds <= 2:
            bucket["under_2s"]["count"] += 1
    for bucket in buckets.values():
        bucket["under_2s"]["denominator"] = bucket["total"]
        if bucket["total"]:
            bucket["under_2s"]["rate"] = bucket["under_2s"]["count"] / bucket["total"]
            bucket["coverage"]["fast_path"] = bucket["fast_path"] / bucket["total"]
            bucket["coverage"]["direct"] = bucket["direct"] / bucket["total"]
            bucket["coverage"]["jev"] = bucket["jev"] / bucket["total"]
        if bucket["eligible"]:
            bucket["coverage"]["eligible"] = bucket["fast_path"] / bucket["eligible"]
        for key, summary in bucket["timings"].items():
            values = summary.pop("_values", [])
            summary["count"] = len(values)
            summary["p50"] = _metric_percentile(values, 0.50)
            summary["p95"] = _metric_percentile(values, 0.95)
            summary["p99"] = _metric_percentile(values, 0.99)
    return buckets


_EVALUATION_TIMEOUT_REASONS = frozenset({
    "deadline", "timeout", "timed_out", "jev_timeout", "provider_timeout",
})


def _evaluation_metric_sample(row):
    """Project one durable event into the bounded public metrics contract."""
    decision = _json_object(row["decision"])
    top_timing = _json_object(decision.get("evaluation_timing"))
    recovery = _json_object(decision.get("recovery"))
    recovery_timing = _json_object(recovery.get("evaluation_timing"))
    state = _safe_state(row["state"])
    recovery_row = (
        state.startswith("recovery")
        or top_timing.get("path") == "recovery"
        or recovery_timing.get("path") == "recovery"
    )
    timing = recovery_timing if recovery_row and recovery_timing else top_timing
    timing = _project_evaluation_timing(timing)
    body = _json_object(row["message_body"])
    historical = (
        not recovery_row
        and timing.get("path") == "historical"
    )
    if not recovery_row and not historical:
        ingestion = body.get("ingestion")
        source = body.get("source")
        historical = (
            ingestion is not None and ingestion != "live"
        ) or (
            source is not None and source not in {"browser", "gateway"}
        )
    bucket = "recovery" if recovery_row else "historical" if historical else "live"
    reason = _safe_text(row["reason"], 4000).lower()
    fallback_reason = timing.get("fallback_reason")
    timed_out = timing.get("timed_out") is True or fallback_reason in _EVALUATION_TIMEOUT_REASONS
    if not timed_out and any(token in reason for token in ("timed out", "timeout", "deadline exceeded")):
        timed_out = True
    return {
        "bucket": bucket,
        "timing": timing,
        "evaluated": bool(timing.get("route") or "model_duration_seconds" in timing or
                          decision.get("action") in {"OPEN", "REDUCE", "CLOSE", "WAIT", "IGNORE", "UPDATE_STOP"}),
        "failed": state in {"error", "recovery_error"},
        "timed_out": timed_out,
    }


def _safe_path(root, raw, default):
    candidate = default
    if isinstance(raw, str) and raw.strip():
        value = Path(raw).expanduser()
        candidate = value if value.is_absolute() else root / value
    try:
        candidate = candidate.resolve(strict=False)
    except OSError:
        return default.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return default.resolve()
    return candidate


def load_dashboard_config(path):
    """Read only the dashboard settings; shadow setup may still be unbound."""
    config_path = Path(path).expanduser().resolve()
    try:
        if config_path.stat().st_size > MAX_BODY:
            raise ValueError("dashboard config is too large")
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("unable to read dashboard config") from exc
    if not isinstance(raw, dict):
        raise ValueError("dashboard config must be an object")
    root = config_path.parent.resolve()
    default_state = root / "state"
    return {
        "path": config_path,
        "root": root,
        "raw": raw,
        "mode": raw.get("mode") if raw.get("mode") in {"paper", "shadow", "live"} else "unknown",
        "database": _safe_path(root, raw.get("database"), default_state / "relay.sqlite3"),
        "kill_switch": _safe_path(root, raw.get("kill_switch"), default_state / "STOP"),
        "runtime_status_file": _safe_path(root, raw.get("runtime_status_file"), default_state / "runtime-status.json"),
    }


class DashboardApp:
    def __init__(self, config, *, enable_setup=False):
        if isinstance(config, (str, Path)):
            self.config_path = Path(config).expanduser().resolve()
            self.config = load_dashboard_config(self.config_path)
        else:
            self.config = config
            self.config_path = Path(config["path"]).expanduser().resolve()
        self._config_lock = threading.RLock()
        self.static_root = (Path(__file__).resolve().parent / "static").resolve()
        self.csrf_token = secrets.token_urlsafe(32)
        self.setup = None
        if enable_setup:
            from .setup import SetupManager
            self.setup = SetupManager(self.config_path)

    def setup_status(self):
        return self.setup.status() | {"csrf_token": self.csrf_token}

    def snapshot(self):
        """Load one coherent config/database/mode snapshot for a request."""
        with self._config_lock:
            try:
                config = load_dashboard_config(self.config_path)
            except (OSError, ValueError) as exc:
                raise ConfigUnavailable("dashboard configuration unavailable") from exc
            self.config = config
            return config

    @property
    def raw(self):
        return self.config["raw"]

    @property
    def mode(self):
        return self.config["mode"]

    def _channels(self, config):
        channels = []
        raw = config["raw"]
        for channel in _json_list(raw.get("channels"))[:50]:
            if not isinstance(channel, dict):
                continue
            role = channel.get("role") if channel.get("role") in {"signals", "context"} else "unknown"
            channels.append({
                "id": _safe_channel_id(channel.get("id")),
                "name": _safe_text(channel.get("name") or channel.get("display_name"), 256),
                "role": role,
                "source_group": _safe_identifier(channel.get("source_group")),
            })
        return channels

    def _ledger(self, config, callback, default):
        path = config.get("database")
        if not isinstance(path, Path) or not path.is_file():
            return default, False
        connection = None
        try:
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not self._binding_matches(config, connection, tables):
                return default, False
            return callback(connection, tables), {"messages", "events", "orders", "positions"}.issubset(tables)
        except (OSError, sqlite3.Error):
            return default, False
        finally:
            if connection is not None:
                connection.close()

    def _binding_matches(self, config, connection, tables):
        if "metadata" not in tables:
            return True
        row = connection.execute("SELECT value FROM metadata WHERE key = 'execution_binding'").fetchone()
        if row is None:
            return True
        binding = _json_object(row[0])
        raw = config["raw"]
        account = raw.get("robinhood", {}).get("account_number") if isinstance(raw.get("robinhood"), dict) else None
        expected_account = None if config["mode"] == "paper" else str(account) if account is not None else None
        return binding.get("mode") == config["mode"] and binding.get("account") == expected_account

    @staticmethod
    def _counts(connection, tables):
        counts = {"messages": 0, "events": 0, "orders": 0, "positions": 0, "held": 0, "errors": 0}
        for table in ("messages", "events", "orders"):
            if table in tables:
                counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        if "positions" in tables:
            counts["positions"] = int(connection.execute("SELECT COUNT(*) FROM positions WHERE quantity > 0").fetchone()[0])
        if "events" in tables:
            counts["held"] = int(connection.execute("SELECT COUNT(*) FROM events WHERE state = 'held'").fetchone()[0])
            counts["errors"] = int(connection.execute("SELECT COUNT(*) FROM events WHERE state IN ('error', 'recovery_error')").fetchone()[0])
        return counts

    def status(self):
        config = self.snapshot()
        raw = config["raw"]

        def read(connection, tables):
            return self._counts(connection, tables)

        counts, available = self._ledger(config, read, {"messages": 0, "events": 0, "orders": 0, "positions": 0, "held": 0, "errors": 0})
        robinhood = raw.get("robinhood") if isinstance(raw.get("robinhood"), dict) else {}
        live_enabled = config["mode"] == "live" and robinhood.get("enable_live_orders") is True and bool(re.fullmatch(r"\d{5,20}", str(robinhood.get("account_number", ""))))
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "mode": config["mode"],
            "live_orders_enabled": live_enabled,
            "kill_switch": bool(config.get("kill_switch") and config["kill_switch"].exists()),
            "ledger_available": available,
            "counts": counts,
            "evaluation_metrics": self.evaluation_metrics(),
            "configured_channels": self._channels(config),
            "runtime": _safe_runtime(raw, config.get("runtime_status_file")),
        }

    def evaluation_metrics(self):
        """Return bounded, read-only aggregates over the latest durable evaluations."""
        config = self.snapshot()
        sample_limit = 1000
        empty = {
            "generated_at": datetime.now(UTC).isoformat(),
            "available": False,
            "sample_limit": sample_limit,
            "sample_count": 0,
            "buckets": _aggregate_evaluation_samples([]),
        }

        def read(connection, tables):
            if "events" not in tables:
                return empty
            message_join = ""
            message_select = "'' AS message_body"
            if "messages" in tables:
                message_join = "LEFT JOIN messages AS m ON m.id = e.message_id AND m.revision = e.revision"
                message_select = "m.body AS message_body"
            rows = connection.execute(
                f"""
                SELECT e.state, e.reason, e.decision, {message_select}
                FROM events AS e
                {message_join}
                ORDER BY e.id DESC
                LIMIT ?
                """,
                (sample_limit,),
            ).fetchall()
            samples = [_evaluation_metric_sample(row) for row in rows]
            return {
                "generated_at": datetime.now(UTC).isoformat(),
                "available": True,
                "sample_limit": sample_limit,
                "sample_count": len(samples),
                "buckets": _aggregate_evaluation_samples(samples),
            }

        result, _ = self._ledger(config, read, empty)
        return result

    def _list_query(self, query, allowed, endpoint):
        params = parse_qs(query, keep_blank_values=True, strict_parsing=True)
        if any(key not in allowed for key in params):
            raise QueryError("unsupported query parameter")
        if any(len(values) != 1 for values in params.values()):
            raise QueryError("duplicate query parameter")
        values = {key: entries[0] for key, entries in params.items()}
        try:
            limit = int(values.get("limit", "50"))
            offset = int(values.get("offset", "0"))
        except ValueError as exc:
            raise QueryError("limit and offset must be integers") from exc
        if not 1 <= limit <= 200 or not 0 <= offset <= MAX_OFFSET:
            raise QueryError("limit or offset is outside the permitted range")
        for key in ("q", "channel_id"):
            if key in values and len(values[key]) > MAX_QUERY_TEXT:
                raise QueryError("filter is too long")
        return values, limit, offset

    @staticmethod
    def _like(value):
        return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    def messages(self, query):
        config = self.snapshot()
        values, limit, offset = self._list_query(query, {"limit", "offset", "q", "channel_id", "state"}, "messages")
        state = values.get("state") or None
        if state and state not in EVENT_STATES:
            raise QueryError("unknown message state")

        def read(connection, tables):
            if "messages" not in tables:
                return {"items": [], "next_offset": None}
            has_events = "events" in tables
            event_join = """
                LEFT JOIN events AS e ON e.id = (
                    SELECT e2.id FROM events AS e2
                    WHERE e2.message_id = m.id AND e2.revision = m.revision
                    ORDER BY e2.id DESC LIMIT 1
                )
            """ if has_events else ""
            event_select = "e.id AS event_id, e.state AS event_state, e.reason AS event_reason, e.decision AS event_decision, e.created_at AS event_created_at" if has_events else "NULL AS event_id, NULL AS event_state, NULL AS event_reason, NULL AS event_decision, NULL AS event_created_at"
            clauses = []
            parameters = []
            if values.get("q"):
                clauses.append("m.body LIKE ? ESCAPE '\\'")
                parameters.append(self._like(values["q"]))
            if values.get("channel_id"):
                clauses.append("m.channel_id = ?")
                parameters.append(values["channel_id"])
            if state:
                if not has_events:
                    return {"items": [], "next_offset": None}
                connection.create_function(
                    "dashboard_event_state", 2,
                    lambda raw_state, raw_decision: _project_event_state(raw_state, _project_decision(raw_decision)),
                )
                clauses.append("e.id IS NOT NULL AND dashboard_event_state(e.state, e.decision) = ?")
                parameters.append(state)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            sql = f"SELECT m.*, {event_select} FROM messages AS m {event_join}{where} ORDER BY m.timestamp DESC, m.id DESC LIMIT ? OFFSET ?"
            rows = connection.execute(sql, (*parameters, limit + 1, offset)).fetchall()
            more = len(rows) > limit
            rows = rows[:limit]
            items = []
            for row in rows:
                event = None
                if row["event_id"] is not None:
                    event = {"id": row["event_id"], "state": row["event_state"], "reason": row["event_reason"], "decision": row["event_decision"], "created_at": row["event_created_at"]}
                items.append(_project_message(row, event))
            return {"items": items, "next_offset": offset + limit if more else None}

        result, _ = self._ledger(config, read, {"items": [], "next_offset": None})
        return result

    def orders(self, query):
        config = self.snapshot()
        values, limit, offset = self._list_query(query, {"limit", "offset", "status"}, "orders")
        status = values.get("status") or None
        if status and status not in ORDER_STATUSES:
            raise QueryError("unknown order status")

        def read(connection, tables):
            if "orders" not in tables:
                return {"items": [], "next_offset": None}
            clauses = []
            parameters = []
            if status:
                clauses.append("status = ?")
                parameters.append(status)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = connection.execute(f"SELECT * FROM orders{where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", (*parameters, limit + 1, offset)).fetchall()
            more = len(rows) > limit
            return {"items": [_project_order(row, config["mode"]) for row in rows[:limit]], "next_offset": offset + limit if more else None}

        result, _ = self._ledger(config, read, {"items": [], "next_offset": None})
        return result

    def events(self, query):
        config = self.snapshot()
        values, limit, offset = self._list_query(query, {"limit", "offset"}, "events")

        def read(connection, tables):
            if "events" not in tables:
                return {"items": [], "next_offset": None}
            rows = connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT ? OFFSET ?", (limit + 1, offset)).fetchall()
            more = len(rows) > limit
            return {"items": [_project_event(row) for row in rows[:limit]], "next_offset": offset + limit if more else None}

        result, _ = self._ledger(config, read, {"items": [], "next_offset": None})
        return result

    def positions(self):
        config = self.snapshot()

        def read(connection, tables):
            if "positions" not in tables:
                return {"items": []}
            if "stop_requests" in tables:
                rows = connection.execute(
                    """
                    SELECT p.*, s.status AS stop_status, s.reason AS stop_reason,
                           s.stop_price AS stop_price, s.order_id AS stop_order_id
                    FROM positions AS p
                    LEFT JOIN stop_requests AS s
                      ON s.source_group = p.source_group AND s.contract = p.contract
                    WHERE p.quantity > 0
                    ORDER BY p.source_group, p.contract
                    """
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM positions WHERE quantity > 0 ORDER BY source_group, contract").fetchall()
            return {"items": [_project_position(row) for row in rows]}

        result, _ = self._ledger(config, read, {"items": []})
        return result

    def account(self):
        """Project persisted account data without any provider calls."""
        config = self.snapshot()

        def read(connection, tables):
            if "metadata" not in tables or config["mode"] == "paper":
                return {}
            row = connection.execute("SELECT value FROM metadata WHERE key=?", (CACHE_KEY,)).fetchone()
            value = _json_object(row[0]) if row else {}
            section = config["raw"].get("robinhood")
            account = section.get("account_number") if isinstance(section, dict) else None
            return value if isinstance(account, str) and account and value.get("account_id") == account else {}

        cached, _ = self._ledger(config, read, {})
        updated_at = _safe_timestamp(cached.get("updated_at"))
        error = cached.get("error")
        error = error if error in (AUTH_ERROR, REFRESH_ERROR) else None
        available = updated_at is not None and cached.get("currency") == "USD" and _account_money(cached.get("equity")) is not None
        age = (datetime.now(UTC) - _parse_iso(updated_at)).total_seconds() if available else None
        positions = []
        for item in _json_list(cached.get("positions")):
            if not isinstance(item, dict):
                continue
            positions.append({
                "contract": _project_contract(item.get("contract")),
                **{key: _account_money(item.get(key)) for key in ("quantity", "average_price", "market_value", "multiplier")},
                "position_type": item.get("position_type") if item.get("position_type") in ("long", "short") else None,
                "quote_timestamp": _safe_timestamp(item.get("quote_timestamp")),
            })
        assets = _json_object(cached.get("asset_values"))
        return {
            "available": available, "status": "error" if error else "ready" if available else "unavailable",
            "updated_at": updated_at, "last_attempt_at": _safe_timestamp(cached.get("last_attempt_at")),
            "error": error, "stale": not available or bool(error) or age < 0 or age >= REFRESH_SECONDS,
            "currency": "USD", "scope": "option_positions", "refresh_interval_seconds": REFRESH_SECONDS,
            **{key: _account_money(cached.get(key)) if available else None for key in ("equity", "cash", "buying_power", "unleveraged_buying_power")},
            "asset_values": {key: _account_money(assets.get(key)) for key in (
                "equity_value", "options_value", "futures_value", "event_contracts_value",
                "crypto_value", "mutual_funds_value", "fixed_income_value",
            )} if available else {},
            "positions": positions if available else [],
        }

    def message_image(self, query):
        values, _, _ = self._list_query(query, {"message_id", "revision", "index"}, "message image")
        message_id, revision = values.get("message_id", ""), values.get("revision", "")
        if not re.fullmatch(r"\d{15,22}", message_id) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", revision):
            raise QueryError("valid message_id and revision are required")
        try:
            index = int(values.get("index", ""))
        except ValueError:
            raise QueryError("image index must be an integer") from None
        if not 0 <= index < MAX_IMAGES:
            raise QueryError("image index is outside the permitted range")

        def read(connection, tables):
            if "messages" not in tables:
                return None
            row = connection.execute("SELECT body FROM messages WHERE id=? AND revision=?", (message_id, revision)).fetchone()
            return dict(_json_object(row[0]), id=message_id) if row else None

        message, _ = self._ledger(self.snapshot(), read, None)
        if message is None:
            return None
        sources = collect_images([message])
        if index >= len(sources):
            return None
        if not IMAGE_PREVIEW_SLOTS.acquire(blocking=False):
            raise ConfigUnavailable("Image previews are busy; try again shortly")
        try:
            with tempfile.TemporaryDirectory(prefix="discord-image-preview-") as directory:
                path = download_images([sources[index]], directory)[0]
                media_type = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}[path.suffix]
                return path.read_bytes(), media_type
        finally:
            IMAGE_PREVIEW_SLOTS.release()

    def static_file(self, request_path):
        if request_path == "/":
            relative = "index.html"
        else:
            segments = request_path.split("/")
            if segments and segments[0] == "":
                segments = segments[1:]
            if segments and segments[0] == "static":
                segments = segments[1:]
            if len(segments) != 1 or not segments[0]:
                return None
            relative = segments[0]
        if not re.fullmatch(r"[A-Za-z0-9._-]+", relative):
            return None
        suffix = Path(relative).suffix.lower()
        if suffix not in {".html", ".css", ".js"}:
            return None
        try:
            candidate = (self.static_root / relative).resolve()
            candidate.relative_to(self.static_root)
        except (OSError, ValueError):
            return None
        return candidate if candidate.is_file() else None


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    @property
    def app(self):
        return self.server.app

    def do_GET(self):
        self._handle(False)

    def do_HEAD(self):
        self._handle(True)

    def do_POST(self):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        routes = {
            "/api/setup/channels", "/api/setup/pause", "/api/setup/reconnect", "/api/setup/mode", "/api/setup/notifications", "/api/setup/expiry-policy", "/api/setup/evaluation", "/api/setup/evaluation/test",
            "/api/setup/discord", "/api/setup/discord/discover",
            "/api/setup/auth/codex/start", "/api/setup/auth/codex/cancel",
            "/api/setup/auth/robinhood/start", "/api/setup/auth/robinhood/cancel",
            "/api/setup/auth/robinhood/callback",
        }
        # Close rejected writes so their unread body cannot become another request.
        self.close_connection = True
        if path not in routes or self.app.setup is None:
            return self._method_not_allowed()
        host, origin = self.headers.get("Host", ""), self.headers.get("Origin", "")
        if (not host or origin not in {"http://" + host, "https://" + host}
                or self.headers.get("Sec-Fetch-Site", "same-origin") not in {"same-origin", "none"}
                or not secrets.compare_digest(self.headers.get("X-Relay-CSRF", "").encode("utf-8"), self.app.csrf_token.encode("ascii"))):
            return self._json(403, {"error": "Open setup in this dashboard before changing settings."}, api=True)
        if self.headers.get_content_type() != "application/json":
            return self._json(415, {"error": "JSON content type required"}, api=True)
        try:
            if parsed.query or self.headers.get("Transfer-Encoding"):
                raise ValueError("Unsupported setup request")
            length = int(self.headers.get("Content-Length", "-1"))
            if not 0 <= length <= 32768:
                return self._json(413, {"error": "Setup request is too large or lacks a length"}, api=True)
            self.connection.settimeout(10)
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete setup request")
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError("Setup request must be an object")
            manager = self.app.setup
            if path == "/api/setup/channels":
                result = manager.save_channels(payload)
            elif path == "/api/setup/notifications":
                result = manager.save_notifications(payload)
            elif path == "/api/setup/discord":
                result = manager.save_discord(payload)
            elif path == "/api/setup/discord/discover":
                result = manager.discover_discord(payload)
            elif path == "/api/setup/mode":
                result = manager.set_mode(payload)
            elif path == "/api/setup/expiry-policy":
                result = manager.set_expiry_policy(payload)
            elif path == "/api/setup/evaluation":
                result = manager.save_evaluation(payload)
            elif path == "/api/setup/evaluation/test":
                result = manager.test_evaluation(payload)
            elif path == "/api/setup/pause":
                if set(payload) != {"paused"} or not isinstance(payload["paused"], bool):
                    raise ValueError("paused must be a boolean")
                result = manager.set_paused(payload["paused"])
            elif path == "/api/setup/reconnect":
                if payload:
                    raise ValueError("Reconnect does not accept parameters")
                result = manager.reconnect()
            elif path == "/api/setup/auth/robinhood/callback":
                result = manager.complete_robinhood_callback(payload)
            else:
                provider, action = path.split("/")[-2:]
                if action == "cancel":
                    if payload:
                        raise ValueError("Cancel does not accept parameters")
                    result = manager.cancel_auth(provider)
                else:
                    result = manager.start_auth(provider, payload)
            self._json(200, (result or {}) | {"csrf_token": self.app.csrf_token}, api=True)
        except (ValueError, TypeError, UnicodeError) as exc:
            self._json(400, {"error": str(exc)}, api=True)
        except RuntimeError as exc:
            self._json(409, {"error": str(exc)}, api=True)
        except Exception:
            self._json(500, {"error": "Setup operation failed; retry or check the account connection."}, api=True)

    def do_PUT(self):
        self.close_connection = True
        self._method_not_allowed()

    do_PATCH = do_PUT
    do_DELETE = do_PUT
    do_OPTIONS = do_PUT

    def _method_not_allowed(self):
        self._json(405, {"error": "method not allowed"}, api=True, extra={"Allow": "GET, HEAD"})

    def _json(self, status, value, *, api=False, extra=None, head=False):
        try:
            body = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            status, body = 500, b'{"error":"dashboard response unavailable"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'")

    def _static(self, path, head):
        candidate = self.app.static_file(path)
        if candidate is None:
            self._json(404, {"error": "not found"}, head=head)
            return
        try:
            body = candidate.read_bytes()
        except OSError:
            self._json(404, {"error": "not found"}, head=head)
            return
        content_type = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8"}[candidate.suffix.lower()]
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._security_headers()
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _handle(self, head):
        try:
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            if "\x00" in path or "\\" in path or any(segment in {".", ".."} for segment in path.split("/")):
                self._json(404, {"error": "not found"}, head=head)
                return
            if path == "/healthz":
                if parsed.query:
                    raise QueryError("healthz does not accept query parameters")
                self._json(200, {"ok": True}, head=head)
                return
            if path == "/api/status":
                if parsed.query:
                    raise QueryError("status does not accept query parameters")
                self._json(200, self.app.status(), api=True, head=head)
                return
            if path == "/api/setup" and self.app.setup is not None:
                if parsed.query:
                    raise QueryError("setup does not accept query parameters")
                self._json(200, self.app.setup_status(), api=True, head=head)
                return
            if path == "/api/setup/robinhood/schemas" and self.app.setup is not None:
                if parsed.query:
                    raise QueryError("schema diagnostics do not accept query parameters")
                self._json(200, self.app.setup.robinhood_schemas(), api=True, head=head)
                return
            if path == "/api/evaluation/metrics":
                if parsed.query:
                    raise QueryError("evaluation metrics does not accept query parameters")
                self._json(200, self.app.evaluation_metrics(), api=True, head=head)
                return
            if path == "/api/messages":
                self._json(200, self.app.messages(parsed.query), api=True, head=head)
                return
            if path == "/api/message-image":
                try:
                    image = self.app.message_image(parsed.query)
                except ImageTransportError as exc:
                    self._json(502, {"error": str(exc), "code": getattr(exc, "code", "image_unavailable")}, api=True, head=head)
                    return
                if image is None:
                    self._json(404, {"error": "Image or message revision is unavailable"}, api=True, head=head)
                    return
                body, media_type = image
                self.send_response(200)
                self.send_header("Content-Type", media_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "private, max-age=300")
                self._security_headers()
                self.end_headers()
                if not head:
                    self.wfile.write(body)
                return
            if path == "/api/orders":
                self._json(200, self.app.orders(parsed.query), api=True, head=head)
                return
            if path == "/api/events":
                self._json(200, self.app.events(parsed.query), api=True, head=head)
                return
            if path == "/api/positions":
                if parsed.query:
                    raise QueryError("positions does not accept query parameters")
                self._json(200, self.app.positions(), api=True, head=head)
                return
            if path == "/api/account":
                if parsed.query:
                    raise QueryError("account does not accept query parameters")
                self._json(200, self.app.account(), api=True, head=head)
                return
            if path.startswith("/api/"):
                self._json(404, {"error": "not found"}, api=True, head=head)
                return
            self._static(path, head)
        except QueryError as exc:
            self._json(400, {"error": str(exc)}, api=True, head=head)
        except ConfigUnavailable as exc:
            self._json(503, {"error": str(exc)}, api=True, head=head)
        except Exception:
            self._json(500, {"error": "dashboard unavailable"}, api=True, head=head)


class DashboardHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, app):
        self.app = app
        super().__init__(server_address, DashboardHandler)

    def server_close(self):
        if self.app.setup is not None:
            self.app.setup.close()
        super().server_close()


def create_server(config, host="127.0.0.1", port=8765, *, enable_setup=False):
    return DashboardHTTPServer((host, int(port)), DashboardApp(config, enable_setup=enable_setup))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.local.json" if Path("config.local.json").exists() else "config.example.json")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--enable-setup", action="store_true", help="enable setup writes behind an authenticated gateway")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    try:
        server = create_server(args.config, args.host, args.port, enable_setup=args.enable_setup)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
