"""Small timing helpers for Discord-facing browser operations."""

from __future__ import annotations

import math
import random
from collections.abc import Callable


def discord_delay(
    base: float,
    *,
    rng: Callable[[float, float], float] | None = None,
) -> float:
    """Return a bounded, slightly varied delay for a Discord operation."""
    try:
        seconds = float(base)
    except (TypeError, ValueError) as exc:
        raise ValueError("base delay must be a positive finite number") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("base delay must be a positive finite number")

    lower = seconds * 0.8
    upper = seconds * 1.2
    sampler = random.uniform if rng is None else rng
    try:
        delay = float(sampler(lower, upper))
    except (TypeError, ValueError) as exc:
        raise ValueError("random delay must be a finite number") from exc
    if not math.isfinite(delay) or not lower <= delay <= upper:
        raise ValueError("random delay must remain within the Discord pacing bounds")
    return delay
