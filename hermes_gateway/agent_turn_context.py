"""Gateway agent-turn context enrichment."""

from __future__ import annotations

import os

from hermes_agent.composition.async_sqlite import run_sqlite_io
from hermes_gateway.config import Platform


class GatewayAgentTurnContextService:
    def __init__(self, runner):
        self._runner = runner

    async def enrich_context_prompt(
        self,
        *,
        context_prompt: str,
        history,
        source,
        event,
        home_target_env_var,
        platform_notice_for,
        voice_runtime_for,
    ) -> str:
        runner = self._runner
        # First-message onboarding -- only on the very first interaction ever
        if not history and not await run_sqlite_io(runner.session_store.has_any_sessions):
            context_prompt += (
                "\n\n[System note: This is the user's very first message ever. "
                "Briefly introduce yourself and mention that /help shows available commands. "
                "Keep the introduction concise -- one or two sentences max.]"
            )
        
        # One-time prompt if no home channel is set for this platform
        # Skip for webhooks - they deliver directly to configured targets (github_comment, etc.)
        if not history and source.platform and source.platform != Platform.LOCAL and source.platform != Platform.WEBHOOK:
            platform_name = source.platform.value
            env_key = home_target_env_var(platform_name)
            if not os.getenv(env_key):
                # Slack dispatches all Hermes commands through a single
                # parent slash command `/hermes`; bare `/sethome` is not
                # registered and would fail with "app did not respond".
                sethome_cmd = (
                    "/hermes sethome"
                    if source.platform == Platform.SLACK
                    else "/sethome"
                )
                notice = (
                    f"📬 No home channel is set for {platform_name.title()}. "
                    f"A home channel is where Hermes delivers cron job results "
                    f"and cross-platform messages.\n\n"
                    f"Type {sethome_cmd} to make this chat your home channel, "
                    f"or ignore to skip."
                )
                await platform_notice_for(runner).deliver_platform_notice(source, notice)
        
        # -----------------------------------------------------------------
        # Voice channel awareness — inject current voice channel state
        # into context so the agent knows who is in the channel and who
        # is speaking, without needing a separate tool call.
        # -----------------------------------------------------------------
        if source.platform == Platform.DISCORD:
            adapter = runner.adapters.get(Platform.DISCORD)
            guild_id = voice_runtime_for(runner).get_guild_id(event)
            if guild_id and adapter and hasattr(adapter, "get_voice_channel_context"):
                vc_context = adapter.get_voice_channel_context(guild_id)
                if vc_context:
                    context_prompt += f"\n\n{vc_context}"


        return context_prompt


def agent_turn_context_for(runner) -> GatewayAgentTurnContextService:
    service = getattr(runner, "agent_turn_context", None)
    if isinstance(service, GatewayAgentTurnContextService):
        return service
    service = GatewayAgentTurnContextService(runner)
    runner.agent_turn_context = service
    return service
