"""Validation and projection for persistent position monitor data."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any


PLAN_FIELDS = frozenset(
    {"duration_seconds", "poll_interval_seconds", "reassess_after_seconds", "conditions"}
)
CONDITION_FIELDS = frozenset({"metric", "comparison", "threshold"})
METRICS = frozenset(
    {"option_bid", "option_ask", "underlying_price", "unrealized_return_fraction", "held_quantity"}
)
SYMBOLS = {"source_day_low", "source_day_high", "entry_premium"}
_SYMBOL_METRICS = {
    "source_day_low": {"underlying_price"},
    "source_day_high": {"underlying_price"},
    "entry_premium": {"option_bid", "option_ask"},
}
_DECIMAL_TEXT = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)\Z")
_MAX_DECIMAL = Decimal("1000000000000")

FACT_FIELDS = frozenset(
    {
        "observed_at",
        "evaluated_at",
        "triggered_at",
        "source_market_date",
        "reference_market_date",
        "market_open",
        "owned_quantity",
        "broker_quantity",
        "held_quantity",
        "average_entry_price",
        "option_bid",
        "option_ask",
        "option_quote_at",
        "underlying_price",
        "underlying_quote_at",
        "source_day_low",
        "source_day_high",
        "unrealized_return_fraction",
        "trigger_reason",
        "triggered_conditions",
        "blockers",
    }
)
_TIMESTAMPS = frozenset(
    {"observed_at", "evaluated_at", "triggered_at", "option_quote_at", "underlying_quote_at"}
)
_DATES = frozenset({"source_market_date", "reference_market_date"})
_NUMBERS = frozenset(
    {
        "average_entry_price",
        "option_bid",
        "option_ask",
        "underlying_price",
        "source_day_low",
        "source_day_high",
        "unrealized_return_fraction",
    }
)
_QUANTITIES = frozenset({"owned_quantity", "broker_quantity", "held_quantity"})


def _decimal_string(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 64 or not _DECIMAL_TEXT.fullmatch(value):
        return False
    try:
        number = Decimal(value)
    except InvalidOperation:
        return False
    return number.is_finite() and abs(number) <= _MAX_DECIMAL


def validate_monitor_plan(value: Any) -> dict[str, Any]:
    """Return a normalized plan or raise ``ValueError`` for an invalid plan."""
    if not isinstance(value, dict) or set(value) != PLAN_FIELDS:
        raise ValueError("monitor plan has missing or unexpected fields")

    duration = value["duration_seconds"]
    interval = value["poll_interval_seconds"]
    reassess = value["reassess_after_seconds"]
    if type(duration) is not int or not 30 <= duration <= 604800:
        raise ValueError("duration_seconds must be an integer from 30 to 604800")
    if type(interval) is not int or not 5 <= interval <= 300:
        raise ValueError("poll_interval_seconds must be an integer from 5 to 300")
    if reassess is not None and (
        type(reassess) is not int or not interval <= reassess <= duration
    ):
        raise ValueError("reassess_after_seconds must be null or within the plan bounds")

    conditions = value["conditions"]
    if not isinstance(conditions, list) or len(conditions) > 8:
        raise ValueError("conditions must be a list of at most 8 entries")
    clean_conditions = []
    for condition in conditions:
        if not isinstance(condition, dict) or set(condition) != CONDITION_FIELDS:
            raise ValueError("monitor condition has missing or unexpected fields")
        metric, comparison, threshold = (
            condition["metric"],
            condition["comparison"],
            condition["threshold"],
        )
        if not isinstance(metric, str) or metric not in METRICS:
            raise ValueError("unknown monitor metric")
        if not isinstance(comparison, str) or comparison not in {"lte", "gte"}:
            raise ValueError("comparison must be lte or gte")
        if isinstance(threshold, str) and threshold in SYMBOLS:
            if metric not in _SYMBOL_METRICS[threshold]:
                raise ValueError("symbolic threshold is incompatible with metric")
        elif not _decimal_string(threshold):
            raise ValueError("threshold must be a bounded decimal string or compatible symbol")
        clean_conditions.append(
            {"metric": metric, "comparison": comparison, "threshold": threshold}
        )

    if not clean_conditions and reassess is None:
        raise ValueError("monitor plan requires a condition or reassessment timer")
    return {
        "duration_seconds": duration,
        "poll_interval_seconds": interval,
        "reassess_after_seconds": reassess,
        "conditions": clean_conditions,
    }


def project_monitor_facts(value: Any) -> dict[str, Any]:
    """Keep only bounded observation facts safe to include in a Codex callback."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in FACT_FIELDS:
        item = value.get(key)
        if item is None:
            continue
        if key in _TIMESTAMPS:
            if not isinstance(item, str) or len(item) > 64:
                continue
            try:
                parsed = datetime.fromisoformat(item.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None:
                continue
            result[key] = item
        elif key in _DATES:
            if isinstance(item, str) and len(item) == 10:
                try:
                    date.fromisoformat(item)
                except ValueError:
                    continue
                result[key] = item
        elif key == "market_open":
            if type(item) is bool:
                result[key] = item
        elif key in _QUANTITIES:
            if type(item) is int and 0 <= item <= 1_000_000_000:
                result[key] = item
        elif key in _NUMBERS:
            if isinstance(item, bool):
                continue
            try:
                number = Decimal(str(item))
            except (InvalidOperation, ValueError):
                continue
            if not number.is_finite() or abs(number) > _MAX_DECIMAL:
                continue
            if key != "unrealized_return_fraction" and number < 0:
                continue
            result[key] = str(number)
        elif key == "trigger_reason":
            if isinstance(item, str) and item in {"condition", "timer", "resume"}:
                result[key] = item
        elif key == "triggered_conditions":
            if (
                isinstance(item, list)
                and len(item) <= 8
                and all(type(index) is int and 0 <= index < 8 for index in item)
                and len(set(item)) == len(item)
            ):
                result[key] = list(item)
        elif key == "blockers":
            if isinstance(item, list):
                result[key] = [
                    text[:300]
                    for text in item[:16]
                    if isinstance(text, str) and text.strip()
                ]
    return result
