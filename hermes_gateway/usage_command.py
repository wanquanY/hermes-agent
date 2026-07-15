"""Gateway /usage command ownership."""

from __future__ import annotations

import asyncio
import logging

from agent.i18n import t
from channels.platforms.base import MessageEvent
from hermes_agent.composition.async_sqlite import run_sqlite_io
from hermes_gateway.agent_cache import AGENT_PENDING_SENTINEL

logger = logging.getLogger(__name__)


def fetch_account_usage(*args, **kwargs):
    from agent.account_usage import fetch_account_usage as _fetch_account_usage

    return _fetch_account_usage(*args, **kwargs)


def render_account_usage_lines(*args, **kwargs):
    from agent.account_usage import render_account_usage_lines as _render_account_usage_lines

    return _render_account_usage_lines(*args, **kwargs)


class GatewayUsageCommandService:
    def __init__(self, runner):
        self._runner = runner

    async def handle_usage_command(self, event: MessageEvent) -> str:
        """Show usage for the current session from running/cached agents or history."""

        source = event.source
        session_key = self._runner._session_key_for_source(source)

        agent = self._runner._running_agents.get(session_key)
        if not agent or agent is AGENT_PENDING_SENTINEL:
            cache_lock = getattr(self._runner, "_agent_cache_lock", None)
            cache = getattr(self._runner, "_agent_cache", None)
            if cache_lock and cache is not None:
                with cache_lock:
                    cached = cache.get(session_key)
                    if cached:
                        agent = cached[0]

        provider = getattr(agent, "provider", None) if agent and agent is not AGENT_PENDING_SENTINEL else None
        base_url = getattr(agent, "base_url", None) if agent and agent is not AGENT_PENDING_SENTINEL else None
        api_key = getattr(agent, "api_key", None) if agent and agent is not AGENT_PENDING_SENTINEL else None
        session_db = getattr(self._runner, "_session_db", None)
        if not provider and session_db is not None:
            try:
                entry_for_billing = await run_sqlite_io(
                    self._runner.session_store.get_or_create_session,
                    source,
                )
                persisted = await run_sqlite_io(
                    session_db.sessions.get,
                    entry_for_billing.session_id,
                ) or {}
            except Exception:
                persisted = {}
            provider = provider or persisted.get("billing_provider")
            base_url = base_url or persisted.get("billing_base_url")

        account_lines: list[str] = []
        if provider:
            try:
                account_snapshot = await asyncio.to_thread(
                    fetch_account_usage,
                    provider,
                    base_url=base_url,
                    api_key=api_key,
                )
            except Exception:
                account_snapshot = None
            if account_snapshot:
                account_lines = render_account_usage_lines(account_snapshot, markdown=True)

        if agent and hasattr(agent, "session_total_tokens") and agent.session_api_calls > 0:
            lines = []

            rl_state = agent.get_rate_limit_state()
            if rl_state and rl_state.has_data:
                from agent.rate_limit_tracker import format_rate_limit_compact
                lines.append(t("gateway.usage.rate_limits", state=format_rate_limit_compact(rl_state)))
                lines.append("")

            input_tokens = getattr(agent, "session_input_tokens", 0) or 0
            output_tokens = getattr(agent, "session_output_tokens", 0) or 0
            cache_read = getattr(agent, "session_cache_read_tokens", 0) or 0
            cache_write = getattr(agent, "session_cache_write_tokens", 0) or 0

            lines.append(t("gateway.usage.header_session"))
            lines.append(t("gateway.usage.label_model", model=agent.model))
            lines.append(t("gateway.usage.label_input_tokens", count=f"{input_tokens:,}"))
            if cache_read:
                lines.append(t("gateway.usage.label_cache_read", count=f"{cache_read:,}"))
            if cache_write:
                lines.append(t("gateway.usage.label_cache_write", count=f"{cache_write:,}"))
            lines.append(t("gateway.usage.label_output_tokens", count=f"{output_tokens:,}"))
            lines.append(t("gateway.usage.label_total", count=f"{agent.session_total_tokens:,}"))
            lines.append(t("gateway.usage.label_api_calls", count=agent.session_api_calls))

            try:
                from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
                cost_result = estimate_usage_cost(
                    agent.model,
                    CanonicalUsage(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_tokens=cache_read,
                        cache_write_tokens=cache_write,
                    ),
                    provider=getattr(agent, "provider", None),
                    base_url=getattr(agent, "base_url", None),
                )
                if cost_result.amount_usd is not None:
                    prefix = "~" if cost_result.status == "estimated" else ""
                    lines.append(t("gateway.usage.label_cost", prefix=prefix, amount=f"{float(cost_result.amount_usd):.4f}"))
                elif cost_result.status == "included":
                    lines.append(t("gateway.usage.label_cost_included"))
            except Exception as exc:
                logger.debug("Could not estimate usage cost for /usage: %s", exc)

            ctx = agent.context_compressor
            if ctx.last_prompt_tokens:
                pct = min(100, ctx.last_prompt_tokens / ctx.context_length * 100) if ctx.context_length else 0
                lines.append(t("gateway.usage.label_context", used=f"{ctx.last_prompt_tokens:,}", total=f"{ctx.context_length:,}", pct=f"{pct:.0f}"))
            if ctx.compression_count:
                lines.append(t("gateway.usage.label_compressions", count=ctx.compression_count))

            if account_lines:
                lines.append("")
                lines.extend(account_lines)

            return "\n".join(lines)

        session_entry = await run_sqlite_io(
            self._runner.session_store.get_or_create_session,
            source,
        )
        history = await run_sqlite_io(
            self._runner.session_store.load_transcript,
            session_entry.session_id,
        )
        if history:
            from agent.model_metadata import estimate_messages_tokens_rough
            msgs = [m for m in history if m.get("role") in {"user", "assistant"} and m.get("content")]
            approx = estimate_messages_tokens_rough(msgs)
            lines = [
                t("gateway.usage.header_session_info"),
                t("gateway.usage.label_messages", count=len(msgs)),
                t("gateway.usage.label_estimated_context", count=f"{approx:,}"),
                t("gateway.usage.detailed_after_first"),
            ]
            if account_lines:
                lines.append("")
                lines.extend(account_lines)
            return "\n".join(lines)
        if account_lines:
            return "\n".join(account_lines)
        return t("gateway.usage.no_data")


def usage_command_for(runner) -> GatewayUsageCommandService:
    service = getattr(runner, "usage_command", None)
    if isinstance(service, GatewayUsageCommandService):
        return service
    service = GatewayUsageCommandService(runner)
    runner.usage_command = service
    return service
