"""Provider routing for Codex, JEV shadow mode, and JEV fallback mode."""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Mapping, Sequence
from typing import Any, Callable

from .interpreter import InterpretationError, validate_decision
from .entry_rules import deterministic_entry
from .jev import (
    JEVError,
    JEVInterpreter,
    JEVResult,
    credential_configured,
    evaluation_settings,
    extract_candidates,
    _valid_answer_shape,
)

ENTRY_JEV_TIMEOUT_MS = 700

_FALLBACK_REASONS = frozenset({
    "candidate_ambiguity",
    "contract_missing",
    "contract_resolution",
    "image_ambiguity",
    "message_ambiguous",
    "unsupported_effect",
    "jev_cooldown",
    "jev_credentials_missing",
    "jev_auth_required",
    "jev_timeout",
    "jev_rate_limited",
    "jev_unavailable",
    "jev_schema_invalid",
    "jev_invalid_decision",
    "jev_provider_failed",
})


def _safe_fallback_reason(error: BaseException) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and code in _FALLBACK_REASONS else "jev_provider_failed"


def _configured_codex_model(config: Mapping[str, Any]) -> str:
    llm = config.get("llm", {}) if isinstance(config, Mapping) else {}
    model = llm.get("model") if isinstance(llm, Mapping) else None
    return model if isinstance(model, str) and model else "codex"


def _merge_timing(
    decision: dict[str, Any],
    metadata: Mapping[str, Any],
    total_seconds: float,
) -> dict[str, Any]:
    timing = dict(decision.get("evaluation_timing") or {})
    timing.update({key: value for key, value in metadata.items() if value is not None})
    timing["model_duration_seconds"] = round(max(0.0, total_seconds), 6)
    return decision | {"evaluation_timing": timing}


class EvaluationRouter:
    """Route one interpretation; account execution remains outside this class."""

    def __init__(
        self,
        config: Mapping[str, Any] | None,
        *,
        codex: object,
        broker: object | None = None,
        on_status: Callable[[dict[str, Any]], Any] | None = None,
    ):
        self.config = config or {}
        self.settings = evaluation_settings(self.config)
        self.codex = codex
        # One Codex operation at a time, including recovery, to preserve
        # serialized provider context. JEV has its own bounded pool below.
        self._codex_lock = asyncio.Lock()
        self._jev_lock = asyncio.Semaphore(2)
        self._closed = False
        self.jev = JEVInterpreter(self.config, broker=broker, on_status=on_status)

    async def _codex_interpret(
        self,
        message: Mapping[str, Any],
        context: Sequence[Mapping[str, Any]],
        positions: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], float]:
        if self._closed:
            raise InterpretationError(
                "evaluation router closed",
                code="internal_error",
                retryable=False,
            )
        started = time.monotonic()
        async with self._codex_lock:
            result = await self.codex.interpret(
                message,
                list(context),
                list(positions),
            )
        duration = max(0.0, time.monotonic() - started)
        if not isinstance(result, dict):
            raise InterpretationError(
                "Codex returned invalid structured output",
                code="invalid_output",
            )
        metadata = {
            key: result[key]
            for key in ("evaluation_timing", "entry_evaluation", "exit_evaluation")
            if key in result
        }
        core_result = {
            key: value
            for key, value in result.items()
            if key not in metadata
        }
        result = validate_decision(core_result, message, list(context)) | metadata
        return result, duration

    @staticmethod
    def _jev_metadata(result: JEVResult, *, route: str) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "evaluator": "jev",
            "route": route,
            "model": result.model,
            "jev_model": result.model,
            "jev_duration_seconds": round(result.duration_seconds, 6),
            "codex_duration_seconds": None,
            "semantic_confidence": result.semantic_confidence,
            "allocation_confidence": result.semantic_confidence,
            "selected_probability": result.selected_probability,
            "native_confidence": result.native_confidence,
            "eligibility_probability": result.eligibility_probability,
        }
        eligible = getattr(result, "eligible", None)
        if isinstance(eligible, bool):
            metadata["eligible"] = eligible
        return metadata

    async def _jev_interpret(
        self,
        message: Mapping[str, Any],
        context: Sequence[Mapping[str, Any]],
        positions: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], JEVResult]:
        started = time.monotonic()
        candidates = extract_candidates(message, context)
        timeout_ms = self.settings["timeout_ms"]
        if not candidates.reason and any(candidate["action"] == "OPEN" for candidate in candidates.candidates):
            timeout_ms = min(timeout_ms, ENTRY_JEV_TIMEOUT_MS)
        try:
            async with asyncio.timeout(timeout_ms / 1000):
                async with self._jev_lock:
                    result = await self.jev.interpret(message, context, positions)
        except TimeoutError:
            raise JEVError("jev_timeout") from None
        if time.monotonic() - started > timeout_ms / 1000:
            raise JEVError("jev_timeout")
        details = result.pop("_jev_result", None)
        if not isinstance(details, JEVResult):
            raise JEVError("jev_schema_invalid")
        if details.expiry_defaulted and isinstance(result.get("contract"), Mapping):
            result["contract"] = dict(result["contract"]) | {"expiry": "nearest"}
        return result, details

    async def _cancel_shadow_task(self, task: asyncio.Task[Any]) -> None:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def interpret(
        self,
        message: Mapping[str, Any],
        context: Sequence[Mapping[str, Any]],
        positions: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        mode = self.settings["mode"]
        started = time.monotonic()
        if self._closed:
            raise InterpretationError("evaluation router closed", code="internal_error", retryable=False)
        if self.settings["direct_entries"] and mode != "jev_shadow":
            decision = deterministic_entry(message)
            if decision is not None:
                elapsed = time.monotonic() - started
                result = _merge_timing(decision, {
                    "evaluator": "rules", "route": "direct", "model": "deterministic-entry-v1",
                    "rules_duration_seconds": round(elapsed, 6),
                    "semantic_confidence": decision["confidence"],
                    "allocation_confidence": decision["confidence"],
                    "fast_path": True, "eligible": True,
                }, elapsed)
                result["evaluation_timing"]["model_duration_seconds"] = 0.0
                return result

        if mode == "codex":
            try:
                decision, codex_seconds = await self._codex_interpret(
                    message, context, positions
                )
            except InterpretationError as error:
                error.evaluation_timing = _merge_timing(
                    {"evaluation_timing": getattr(error, "evaluation_timing", None)},
                    {
                        "evaluator": "codex",
                        "route": "codex",
                        "model": _configured_codex_model(self.config),
                        "codex_duration_seconds": max(
                            0.0, time.monotonic() - started
                        ),
                    },
                    max(0.0, time.monotonic() - started),
                )["evaluation_timing"]
                raise
            return _merge_timing(
                decision,
                {
                    "evaluator": "codex",
                    "route": "codex",
                    "model": _configured_codex_model(self.config),
                    "codex_duration_seconds": codex_seconds,
                },
                time.monotonic() - started,
            )

        if mode == "jev_shadow":
            jev_task = asyncio.create_task(
                self._jev_interpret(message, context, positions)
            )
            try:
                codex_result, codex_seconds = await self._codex_interpret(
                    message, context, positions
                )
            except BaseException:
                await self._cancel_shadow_task(jev_task)
                raise

            jev_result: JEVResult | None = None
            shadow_reason = None
            try:
                _shadow_decision, jev_result = await jev_task
            except asyncio.CancelledError:
                await self._cancel_shadow_task(jev_task)
                raise
            except Exception as error:
                # Shadow evaluation is diagnostic; Codex remains authoritative.
                shadow_reason = _safe_fallback_reason(error) if isinstance(error, JEVError) else "jev_provider_failed"

            codex_model = _configured_codex_model(self.config)
            metadata: dict[str, Any] = {
                "evaluator": "codex",
                "route": "shadow",
                "model": codex_model,
                "codex_duration_seconds": codex_seconds,
                "allocation_confidence": codex_result["confidence"],
            }
            if jev_result is not None:
                metadata.update(self._jev_metadata(jev_result, route="shadow"))
                # Keep Codex as the authoritative evaluator/model in shadow mode.
                metadata["evaluator"] = "codex"
                metadata["model"] = codex_model
                metadata["codex_duration_seconds"] = codex_seconds
                metadata["allocation_confidence"] = codex_result["confidence"]
                metadata["jev_shadow_action"] = _shadow_decision["action"]
                metadata["jev_shadow_agrees"] = all(_shadow_decision.get(key) == codex_result.get(key)
                    for key in ("action", "contract", "quantity", "fraction", "profit_only"))
            elif shadow_reason:
                metadata["fallback_reason"] = shadow_reason
            return _merge_timing(
                codex_result,
                metadata,
                time.monotonic() - started,
            )

        jev_started = time.monotonic()
        jev_eligible: bool | None = None
        try:
            decision, jev_result = await self._jev_interpret(
                message, context, positions
            )
            return _merge_timing(
                decision,
                self._jev_metadata(jev_result, route="jev"),
                time.monotonic() - started,
            )
        except JEVError as jev_error:
            fallback_reason = _safe_fallback_reason(jev_error)
            jev_eligible = getattr(jev_error, "eligible", None)
        except asyncio.CancelledError:
            raise
        except KeyboardInterrupt:
            raise
        except Exception:
            fallback_reason = "jev_provider_failed"
            jev_eligible = None
        jev_seconds = max(0.0, time.monotonic() - jev_started)

        try:
            decision, codex_seconds = await self._codex_interpret(
                message, context, positions
            )
        except InterpretationError as error:
            metadata: dict[str, Any] = {
                "evaluator": "codex",
                "route": "fallback",
                "model": _configured_codex_model(self.config),
                "fallback_reason": fallback_reason,
                "jev_duration_seconds": round(jev_seconds, 6),
                "codex_duration_seconds": max(
                    0.0, time.monotonic() - started - jev_seconds
                ),
            }
            if isinstance(jev_eligible, bool):
                metadata["eligible"] = jev_eligible
            error.evaluation_timing = _merge_timing(
                {"evaluation_timing": getattr(error, "evaluation_timing", None)},
                metadata,
                max(0.0, time.monotonic() - started),
            )["evaluation_timing"]
            raise

        metadata = {
            "evaluator": "codex",
            "route": "fallback",
            "model": _configured_codex_model(self.config),
            "fallback_reason": fallback_reason,
            "jev_duration_seconds": round(jev_seconds, 6),
            "codex_duration_seconds": codex_seconds,
        }
        if isinstance(jev_eligible, bool):
            metadata["eligible"] = jev_eligible
        return _merge_timing(
            decision,
            metadata,
            time.monotonic() - started,
        )

    async def assess_recovery(
        self,
        message: Mapping[str, Any],
        context: Sequence[Mapping[str, Any]],
        positions: Sequence[Mapping[str, Any]],
        decision: Mapping[str, Any],
        facts: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Recovery always uses serialized Codex context; JEV is text-only fast path."""
        if self._closed:
            raise InterpretationError(
                "evaluation router closed",
                code="internal_error",
                retryable=False,
            )
        async with self._codex_lock:
            return await self.codex.assess_recovery(
                message,
                list(context),
                list(positions),
                dict(decision),
                dict(facts),
            )

    async def aclose(self) -> None:
        self._closed = True
        await self.jev.aclose()


async def synthetic_test(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Run one explicit synthetic classification, without messages or broker access."""
    settings = evaluation_settings(config or {})
    provider = JEVInterpreter(config)
    started = time.monotonic()
    result = {"provider": "jev", "model": settings["model"], "synthetic": True,
              "configured": credential_configured(config or {})}
    try:
        key = provider._api_key()
        if not key:
            raise JEVError("jev_credentials_missing")
        payload = json.dumps({"model": settings["model"], "state": "Synthetic connection check. Status: online.",
            "questions": {"connectivity": {"type": "choice", "instructions": "Choose the status stated in the text.",
                "criteria": {"online": "The status is online.", "offline": "The status is offline."}}}}).encode()
        async with asyncio.timeout(settings["timeout_ms"] / 1000):
            response = await provider._request_once(payload, key)
        answers = response.get("answers")
        answer = answers.get("connectivity") if isinstance(answers, Mapping) else None
        if not _valid_answer_shape(answer, {"online", "offline"}) or answer["choice"] != "online":
            raise JEVError("jev_schema_invalid")
        result.update(state="connected", detail="TypeSafe answered the synthetic classification; no trading action was evaluated or submitted.")
    except (JEVError, TimeoutError) as error:
        code = getattr(error, "code", "jev_timeout")
        result.update(state="auth_required" if code in {"jev_credentials_missing", "jev_auth_required"} else "unavailable",
                      detail=str(error) if isinstance(error, JEVError) else "TypeSafe synthetic check exceeded its deadline.")
    finally:
        await provider.aclose()
    result["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
    return result


test_connection = synthetic_test
evaluate_synthetic = synthetic_test


__all__ = [
    "EvaluationRouter",
    "credential_configured",
    "evaluation_settings",
    "synthetic_test",
    "test_connection",
]
