"""Gateway inbound message turn coordinator."""

from __future__ import annotations

import dataclasses
import logging
import time
from typing import Optional

from channels.platforms.base import MessageEvent
from agent.i18n import t
from hermes_agent.application.active_work_registry import ActiveWorkRegistry, WorkRejected
from hermes_agent.composition.async_sqlite import run_sqlite_io
from hermes_constants import get_hermes_home
from hermes_gateway.agent_cache import AGENT_PENDING_SENTINEL
from hermes_gateway.busy_message_runtime import busy_message_for
from hermes_gateway.goal_commands import goal_command_for
from hermes_gateway.message_command_runtime import message_command_for
from hermes_gateway.message_ingress import message_ingress_for
from hermes_gateway.model_command import model_command_for
from hermes_gateway.profile_runtime import UnknownProfileError, profile_runtime_for
from hermes_gateway.runtime_status_writer import runtime_status_for
from hermes_gateway.session_runtime_state import session_runtime_state_for
from hermes_gateway.session_turn_lease import session_turn_lease_for

logger = logging.getLogger(__name__)


class GatewayMessageRuntime:
    """Owns per-message pre-agent coordination for the gateway runner."""

    def __init__(self, runner):
        self._runner = runner

    async def handle_message(self, event: MessageEvent) -> Optional[str]:
        profile_runtime = profile_runtime_for(self._runner)
        try:
            source = profile_runtime.route_source(event.source)
        except UnknownProfileError:
            logger.error("Rejected inbound source with an unavailable profile", exc_info=True)
            return None
        if source is not event.source:
            event = dataclasses.replace(event, source=source)
        with profile_runtime.scope_for_source(source):
            return await self._handle_scoped_message(event)

    async def _handle_scoped_message(self, event: MessageEvent) -> Optional[str]:
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

        work_id = f"gateway:{session_key}:{time.time_ns()}"

        async def _persist_timeout() -> None:
            await run_sqlite_io(
                runner.session_store.mark_resume_pending,
                session_key,
                "runtime_drain_timeout",
            )

        def _cancel_turn() -> None:
            agent = runner._running_agents.get(session_key)
            if agent is not None and agent is not AGENT_PENDING_SENTINEL:
                interrupt = getattr(agent, "interrupt", None)
                if callable(interrupt):
                    interrupt("Gateway runtime drain timeout")

        try:
            work_lease = runner._active_work_registry.register(
                kind="conversation_turn",
                surface="gateway",
                work_id=work_id,
                metadata={"session_key": session_key},
                persist_timeout=_persist_timeout,
                cancel=_cancel_turn,
            )
        except WorkRejected:
            return t("gateway.draining", count=runner._running_agent_count())

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
            try:
                model_command_for(runner).restore_pending_one_turn_model_override(
                    session_key
                )
            except Exception:
                logger.debug(
                    "Failed to restore one-turn model override for %s",
                    session_key,
                    exc_info=True,
                )
            session_runtime_state_for(runner).release_running_agent_state(session_key)
            session_turn_lease_for(runner).release(session_key, run_generation)
            work_lease.release()

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
    # GatewayRunner initialization installs the process registry in
    # production. Lightweight embedders and test doubles can bypass that
    # initializer, so give each such runner an isolated lifecycle owner
    # instead of silently sharing process-global state.
    if getattr(runner, "_active_work_registry", None) is None:
        runner._active_work_registry = ActiveWorkRegistry()
    service = getattr(runner, "message_runtime", None)
    if isinstance(service, GatewayMessageRuntime):
        return service
    service = GatewayMessageRuntime(runner)
    runner.message_runtime = service
    return service
