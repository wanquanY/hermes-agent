"""Request-local attribution for auxiliary model responses.

Auxiliary calls do not own session persistence.  The active agent is published
through a ContextVar at turn entry, and the auxiliary response-validation seam
forwards usage to ``model_usage_recorder``, the local single accounting owner.
"""

from __future__ import annotations

from contextvars import ContextVar
import logging
from typing import Any, Optional


logger = logging.getLogger(__name__)

_accounting: ContextVar[tuple[Any | None, Any | None, str | None] | None] = (
    ContextVar("aux_accounting_context", default=None)
)
_EXCLUDED_TASKS = frozenset({"moa_reference", "moa_aggregator"})


def set_accounting_context(
    session_db: Any = None,
    session_id: Optional[str] = None,
    *,
    agent: Any = None,
):
    """Publish the active agent, with a legacy DB pair as compatibility data."""
    if agent is not None:
        return _accounting.set((agent, None, None))
    if session_db is None or not session_id:
        return _accounting.set(None)
    return _accounting.set((None, session_db, session_id))


def reset_accounting_context(token) -> None:
    try:
        _accounting.reset(token)
    except Exception:
        _accounting.set(None)


def get_accounting_context() -> tuple[Any | None, Any | None, str | None] | None:
    return _accounting.get()


def record_aux_usage(
    response: Any,
    task: Optional[str],
    *,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
) -> None:
    """Attribute a successful auxiliary response without affecting its result."""
    try:
        normalized_task = str(task or "").strip()
        if not normalized_task or normalized_task in _EXCLUDED_TASKS:
            return
        context = _accounting.get()
        raw_usage = getattr(response, "usage", None)
        if context is None or raw_usage is None:
            return
        agent, session_db, session_id = context
        model = str(getattr(response, "model", "") or "unknown")
        if agent is not None:
            from agent.model_usage_recorder import (
                ModelUsageAttribution,
                record_model_response_usage,
            )

            record_model_response_usage(
                agent,
                raw_usage,
                attribution=ModelUsageAttribution(
                    purpose=f"aux.{normalized_task}",
                    model=model,
                    provider=str(provider or ""),
                    base_url=str(base_url or ""),
                    primary=False,
                ),
            )
            return
        if session_db is not None and session_id and hasattr(
            session_db,
            "record_auxiliary_usage",
        ):
            from agent.usage_pricing import estimate_usage_cost, normalize_usage

            usage = normalize_usage(raw_usage, provider=provider)
            if not usage.total_tokens:
                return
            estimated_cost = estimate_usage_cost(
                model,
                usage,
                provider=provider,
                base_url=base_url,
                allow_network_discovery=False,
            ).amount_usd
            session_db.record_auxiliary_usage(
                session_id,
                normalized_task,
                model=model,
                billing_provider=provider,
                billing_base_url=base_url,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                estimated_cost_usd=(
                    float(estimated_cost) if estimated_cost is not None else None
                ),
            )
    except Exception:
        logger.debug("auxiliary usage recording failed open", exc_info=True)


__all__ = [
    "get_accounting_context",
    "record_aux_usage",
    "reset_accounting_context",
    "set_accounting_context",
]
