"""Single application owner for per-response model usage accounting."""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any

from agent.usage_pricing import (
    CanonicalUsage,
    CostResult,
    estimate_usage_cost,
    normalize_usage,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelUsageAttribution:
    purpose: str
    model: str
    provider: str
    base_url: str = ""
    api_mode: str = "chat_completions"
    primary: bool = False


@dataclass(frozen=True)
class ModelUsageRecord:
    attribution: ModelUsageAttribution
    usage: CanonicalUsage
    cost: CostResult | None
    reported_total_tokens: int

    @property
    def usage_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.usage.prompt_tokens,
            "completion_tokens": self.usage.output_tokens,
            "total_tokens": self.reported_total_tokens,
            "input_tokens": self.usage.input_tokens,
            "output_tokens": self.usage.output_tokens,
            "cache_read_tokens": self.usage.cache_read_tokens,
            "cache_write_tokens": self.usage.cache_write_tokens,
            "reasoning_tokens": self.usage.reasoning_tokens,
        }


def _usage_lock(agent: Any) -> threading.RLock:
    lock = getattr(agent, "_model_usage_lock", None)
    if lock is None:
        lock = threading.RLock()
        setattr(agent, "_model_usage_lock", lock)
    return lock


def _increment(agent: Any, attribute: str, amount: int | float) -> None:
    setattr(agent, attribute, getattr(agent, attribute, 0) + amount)


def _record_for_observability(agent: Any, record: ModelUsageRecord) -> None:
    records = getattr(agent, "_model_usage_records", None)
    if not isinstance(records, deque):
        records = deque(maxlen=128)
        setattr(agent, "_model_usage_records", records)
    records.append(record)


def record_model_response_usage(
    agent: Any,
    response_usage: Any,
    *,
    attribution: ModelUsageAttribution,
    canonical_usage: CanonicalUsage | None = None,
    reported_total_tokens: int | None = None,
    update_context: bool = False,
) -> ModelUsageRecord:
    """Record exactly one provider response in memory and SQLite.

    The response count is recorded even when the provider omits token usage.
    Auxiliary calls contribute totals and costs without replacing the primary
    session model or billing route.
    """
    usage = canonical_usage or normalize_usage(
        response_usage,
        provider=attribution.provider,
        api_mode=attribution.api_mode,
    )
    total_tokens = max(
        int(reported_total_tokens or 0),
        usage.total_tokens,
    )
    cost = (
        estimate_usage_cost(
            attribution.model,
            usage,
            provider=attribution.provider,
            base_url=attribution.base_url,
            api_key=getattr(agent, "api_key", ""),
        )
        if response_usage or usage.total_tokens
        else None
    )
    record = ModelUsageRecord(
        attribution=attribution,
        usage=usage,
        cost=cost,
        reported_total_tokens=total_tokens,
    )

    with _usage_lock(agent):
        _increment(agent, "session_api_calls", 1)
        _increment(agent, "session_prompt_tokens", usage.prompt_tokens)
        _increment(agent, "session_completion_tokens", usage.output_tokens)
        _increment(agent, "session_total_tokens", total_tokens)
        _increment(agent, "session_input_tokens", usage.input_tokens)
        _increment(agent, "session_output_tokens", usage.output_tokens)
        _increment(agent, "session_cache_read_tokens", usage.cache_read_tokens)
        _increment(agent, "session_cache_write_tokens", usage.cache_write_tokens)
        _increment(agent, "session_reasoning_tokens", usage.reasoning_tokens)

        if cost is not None:
            if cost.amount_usd is not None:
                _increment(
                    agent,
                    "session_estimated_cost_usd",
                    float(cost.amount_usd),
                )
            agent.session_cost_status = cost.status
            agent.session_cost_source = cost.source

        if update_context:
            compressor = getattr(agent, "context_compressor", None)
            if compressor is not None:
                try:
                    compressor.update_from_response(record.usage_dict)
                except Exception:
                    logger.debug("context usage update failed", exc_info=True)

        session_db = getattr(agent, "_session_db", None)
        session_id = str(getattr(agent, "session_id", "") or "")
        if session_db is not None and session_id:
            try:
                if not getattr(agent, "_session_db_created", False):
                    agent._ensure_db_session()
                db_fields: dict[str, Any] = {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_read_tokens": usage.cache_read_tokens,
                    "cache_write_tokens": usage.cache_write_tokens,
                    "reasoning_tokens": usage.reasoning_tokens,
                    "estimated_cost_usd": (
                        float(cost.amount_usd)
                        if cost is not None and cost.amount_usd is not None
                        else None
                    ),
                    "cost_status": cost.status if cost is not None else None,
                    "cost_source": cost.source if cost is not None else None,
                    "pricing_version": (
                        cost.pricing_version if cost is not None else None
                    ),
                    "api_call_count": 1,
                }
                if attribution.primary:
                    db_fields.update(
                        {
                            "billing_provider": attribution.provider,
                            "billing_base_url": attribution.base_url,
                            "billing_mode": (
                                "subscription_included"
                                if cost is not None and cost.status == "included"
                                else None
                            ),
                            "model": attribution.model or None,
                        }
                    )
                session_db.sessions.update_token_counts(session_id, **db_fields)
            except Exception as exc:
                logger.debug(
                    "model usage persistence failed "
                    "(session=%s, purpose=%s, model=%s, tokens=%d): %s",
                    session_id,
                    attribution.purpose,
                    attribution.model,
                    total_tokens,
                    exc,
                )

        _record_for_observability(agent, record)

    logger.info(
        "model usage: purpose=%s model=%s provider=%s in=%d out=%d "
        "cache_read=%d cache_write=%d reasoning=%d total=%d",
        attribution.purpose,
        attribution.model,
        attribution.provider or "unknown",
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
        usage.reasoning_tokens,
        total_tokens,
    )
    return record


__all__ = [
    "ModelUsageAttribution",
    "ModelUsageRecord",
    "record_model_response_usage",
]
