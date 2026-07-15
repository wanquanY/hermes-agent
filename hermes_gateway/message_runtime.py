"""Gateway inbound message turn coordinator."""

from __future__ import annotations

import logging
import time
from typing import Optional

from channels.platforms.base import MessageEvent
from hermes_agent.composition.async_sqlite import run_sqlite_io
from hermes_constants import get_hermes_home
from hermes_gateway.agent_cache import AGENT_PENDING_SENTINEL
from hermes_gateway.busy_message_runtime import busy_message_for
from hermes_gateway.goal_commands import goal_command_for
from hermes_gateway.message_command_runtime import message_command_for
from hermes_gateway.message_ingress import message_ingress_for
from hermes_gateway.runtime_status_writer import runtime_status_for
from hermes_gateway.session_runtime_state import session_runtime_state_for

logger = logging.getLogger(__name__)


class GatewayMessageRuntime:
    """Owns per-message pre-agent coordination for the gateway runner."""

    def __init__(self, runner):
        self._runner = runner

    async def handle_message(self, event: MessageEvent) -> Optional[str]:
        runner = self._runner
        ingress = await message_ingress_for(runner).preprocess(
            event,
            hermes_home=get_hermes_home(),
        )
        if ingress.action == "respond":
            return ingress.response
        event = ingress.event
        source = ingress.source
        session_key = ingress.session_key

        busy_result = await busy_message_for(runner).handle_if_busy(event, source, session_key)
        if busy_result.handled:
            return busy_result.response

        command_result = await message_command_for(runner).dispatch(event, source, session_key)
        if getattr(command_result, "handled", True):
            return getattr(command_result, "response", command_result)

        runner._running_agents[session_key] = AGENT_PENDING_SENTINEL
        runner._running_agents_ts[session_key] = time.time()
        runtime_status_for(runner).persist_active_agents()
        run_generation = session_runtime_state_for(runner).begin_session_run_generation(session_key)

        try:
            agent_result = await runner._handle_message_with_agent(
                event,
                source,
                session_key,
                run_generation,
            )
            await self._continue_goal_if_needed(agent_result, source)
            return agent_result
        finally:
            session_runtime_state_for(runner).release_running_agent_state(session_key)

    async def _continue_goal_if_needed(self, agent_result, source) -> None:
        runner = self._runner
        try:
            final_text = ""
            if isinstance(agent_result, dict):
                final_text = str(agent_result.get("final_response") or "")
            elif isinstance(agent_result, str):
                final_text = agent_result
            if not final_text.strip():
                return
            try:
                session_entry = await run_sqlite_io(
                    runner.session_store.get_or_create_session,
                    source,
                )
            except Exception:
                session_entry = None
            if session_entry is None:
                return
            await goal_command_for(runner).post_turn_goal_continuation(
                session_entry=session_entry,
                source=source,
                final_response=final_text,
            )
        except Exception as exc:
            logger.debug("goal continuation hook failed: %s", exc)


def message_runtime_for(runner) -> GatewayMessageRuntime:
    service = getattr(runner, "message_runtime", None)
    if isinstance(service, GatewayMessageRuntime):
        return service
    service = GatewayMessageRuntime(runner)
    runner.message_runtime = service
    return service
