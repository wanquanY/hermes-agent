"""Gateway /compress command ownership."""

from __future__ import annotations

import asyncio
import logging

from agent.i18n import t
from channels.platforms.base import MessageEvent
from hermes_agent.composition.async_sqlite import run_sqlite_io
from hermes_gateway.agent_cache import agent_cache_for
from hermes_gateway.gateway_runtime_config import runtime_config_for

logger = logging.getLogger(__name__)


class GatewayCompressCommandMixin:
    async def _handle_compress_command(self, event: MessageEvent) -> str:
        """Manually compress the current conversation transcript."""

        source = event.source
        session_entry = await run_sqlite_io(
            self.session_store.get_or_create_session,
            source,
        )
        history = await run_sqlite_io(
            self.session_store.load_transcript,
            session_entry.session_id,
        )

        if not history or len(history) < 4:
            return t("gateway.compress.not_enough")

        focus_topic = (event.get_command_args() or "").strip() or None

        try:
            from run_agent import AIAgent
            from agent.manual_compression_feedback import summarize_manual_compression
            from agent.model_metadata import estimate_request_tokens_rough

            session_key = self._session_key_for_source(source)
            model, runtime_kwargs = runtime_config_for(self).resolve_session_agent_runtime(
                source=source,
                session_key=session_key,
            )
            if not runtime_kwargs.get("api_key"):
                return t("gateway.compress.no_provider")

            msgs = [
                {"role": m.get("role"), "content": m.get("content")}
                for m in history
                if m.get("role") in {"user", "assistant"} and m.get("content")
            ]

            tmp_agent = AIAgent(
                **runtime_kwargs,
                model=model,
                max_iterations=4,
                quiet_mode=True,
                skip_memory=True,
                enabled_toolsets=["memory"],
                session_id=session_entry.session_id,
            )
            try:
                tmp_agent._print_fn = lambda *a, **kw: None

                sys_prompt = getattr(tmp_agent, "_cached_system_prompt", "") or ""
                tools = getattr(tmp_agent, "tools", None) or None
                approx_tokens = estimate_request_tokens_rough(
                    msgs, system_prompt=sys_prompt, tools=tools
                )

                compressor = tmp_agent.context_compressor
                if not compressor.has_content_to_compress(msgs):
                    return t("gateway.compress.nothing_to_do")

                loop = asyncio.get_running_loop()
                compressed, _ = await loop.run_in_executor(
                    None,
                    lambda: tmp_agent._compress_context(
                        msgs,
                        "",
                        approx_tokens=approx_tokens,
                        focus_topic=focus_topic,
                        force=True,
                    ),
                )

                new_session_id = tmp_agent.session_id
                if new_session_id != session_entry.session_id:
                    await run_sqlite_io(
                        self.session_store.update_entry_session_id,
                        session_entry.session_key,
                        new_session_id,
                    )

                await run_sqlite_io(
                    self.session_store.rewrite_transcript,
                    new_session_id,
                    compressed,
                )
                await run_sqlite_io(
                    self.session_store.update_session,
                    session_entry.session_key,
                    last_prompt_tokens=0,
                )
                new_tokens = estimate_request_tokens_rough(
                    compressed, system_prompt=sys_prompt, tools=tools
                )
                summary = summarize_manual_compression(
                    msgs,
                    compressed,
                    approx_tokens,
                    new_tokens,
                )
                summary_aborted = bool(getattr(compressor, "_last_compress_aborted", False))
                summary_err = getattr(compressor, "_last_summary_error", None)
                aux_fail_model = getattr(compressor, "_last_aux_model_failure_model", None)
                aux_fail_err = getattr(compressor, "_last_aux_model_failure_error", None)
            finally:
                agent_cache_for(self).evict_cached_agent(session_key)
                self._cleanup_agent_resources(tmp_agent)
            lines = [f"🗜️ {summary['headline']}"]
            if focus_topic:
                lines.append(t("gateway.compress.focus_line", topic=focus_topic))
            lines.append(summary["token_line"])
            if summary["note"]:
                lines.append(summary["note"])
            if summary_aborted:
                lines.append(
                    t(
                        "gateway.compress.aborted",
                        error=(summary_err or "unknown error"),
                    )
                )
            elif aux_fail_model:
                lines.append(
                    t(
                        "gateway.compress.aux_failed",
                        model=aux_fail_model,
                        error=(aux_fail_err or "unknown error"),
                    )
                )
            return "\n".join(lines)
        except Exception as e:
            logger.warning("Manual compress failed: %s", e)
            return t("gateway.compress.failed", error=e)
