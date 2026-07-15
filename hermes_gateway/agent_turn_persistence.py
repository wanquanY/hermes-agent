"""Persistence policy for completed Gateway agent turns."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from hermes_agent.composition.async_sqlite import run_sqlite_io
from hermes_agent.gateway.runtime_config import resolve_gateway_model
from hermes_gateway.agent_cache import agent_cache_for
from hermes_gateway.gateway_runtime_config import runtime_config_for


logger = logging.getLogger(__name__)

_CONTEXT_OVERFLOW_MARKERS = (
    "context length",
    "context size",
    "context window",
    "maximum context",
    "token limit",
    "too many tokens",
    "reduce the length",
    "exceeds the limit",
    "request entity too large",
    "prompt is too long",
    "payload too large",
    "input is too long",
)


class GatewayAgentTurnPersistenceService:
    """Classify and persist one completed turn through a single DB crossing."""

    def __init__(self, runner) -> None:
        self._runner = runner

    async def persist(
        self,
        *,
        event: Any,
        source: Any,
        session_entry: Any,
        session_key: str,
        history: list[dict[str, Any]],
        message_text: str,
        response: str,
        agent_result: dict[str, Any],
        agent_messages: list[dict[str, Any]],
    ) -> str:
        runner = self._runner
        agent_failed_early = bool(agent_result.get("failed"))
        error_text = str(agent_result.get("error", "")).lower()
        context_overflow = agent_failed_early and (
            bool(agent_result.get("compression_exhausted"))
            or any(marker in error_text for marker in _CONTEXT_OVERFLOW_MARKERS)
            or ("400" in error_text and len(history) > 50)
        )
        if context_overflow:
            logger.info(
                "Skipping transcript persistence for context-overflow failure "
                "in session %s to prevent session growth loop.",
                session_entry.session_id,
            )
        elif agent_failed_early:
            logger.info(
                "Transient agent failure in session %s — persisting user "
                "message so conversation context is preserved on retry.",
                session_entry.session_id,
            )

        compression_exhausted = bool(agent_result.get("compression_exhausted"))
        if compression_exhausted:
            logger.info(
                "Auto-resetting session %s after compression exhaustion.",
                session_entry.session_id,
            )

        timestamp = datetime.now().isoformat()
        agent_persisted = runner._session_db is not None
        transcript_writes = self._transcript_writes(
            event=event,
            source=source,
            history=history,
            message_text=message_text,
            response=response,
            agent_result=agent_result,
            agent_messages=agent_messages,
            timestamp=timestamp,
            context_overflow=context_overflow,
            agent_failed_early=agent_failed_early,
            agent_persisted=agent_persisted,
        )

        def _persist_turn() -> None:
            if compression_exhausted and session_entry and session_key:
                runner.session_store.reset_session(session_key)
            for entry, skip_db in transcript_writes:
                if entry.get("role") == "session_meta":
                    entry["model"] = resolve_gateway_model()
                runner.session_store.append_to_transcript(
                    session_entry.session_id,
                    entry,
                    skip_db=skip_db,
                )
            runner.session_store.update_session(
                session_entry.session_key,
                last_prompt_tokens=agent_result.get("last_prompt_tokens", 0),
            )

        await run_sqlite_io(_persist_turn)

        if compression_exhausted and session_entry and session_key:
            agent_cache_for(runner).evict_cached_agent(session_key)
            runner._session_model_overrides.pop(session_key, None)
            runtime_config_for(runner).set_session_reasoning_override(session_key, None)
            if hasattr(runner, "_pending_model_notes"):
                runner._pending_model_notes.pop(session_key, None)
            response = (response or "") + (
                "\n\n🔄 Session auto-reset — the conversation exceeded the "
                "maximum context size and could not be compressed further. "
                "Your next message will start a fresh session."
            )
        return response

    @staticmethod
    def _transcript_writes(
        *,
        event: Any,
        source: Any,
        history: list[dict[str, Any]],
        message_text: str,
        response: str,
        agent_result: dict[str, Any],
        agent_messages: list[dict[str, Any]],
        timestamp: str,
        context_overflow: bool,
        agent_failed_early: bool,
        agent_persisted: bool,
    ) -> list[tuple[dict[str, Any], bool]]:
        if context_overflow:
            return []

        writes: list[tuple[dict[str, Any], bool]] = []
        if not history:
            writes.append(
                (
                    {
                        "role": "session_meta",
                        "tools": agent_result.get("tools", []) or [],
                        "platform": source.platform.value
                        if source.platform
                        else "",
                        "timestamp": timestamp,
                    },
                    False,
                )
            )

        if agent_failed_early:
            writes.append(
                (
                    _user_entry(message_text, event.message_id, timestamp),
                    agent_persisted,
                )
            )
            return writes

        history_len = agent_result.get("history_offset", len(history))
        new_messages = (
            agent_messages[history_len:]
            if len(agent_messages) > history_len
            else []
        )
        if not new_messages:
            writes.append(
                (
                    _user_entry(message_text, event.message_id, timestamp),
                    agent_persisted,
                )
            )
            if response:
                writes.append(
                    (
                        {"role": "assistant", "content": response, "timestamp": timestamp},
                        agent_persisted,
                    )
                )
            return writes

        user_message_id_attached = False
        for message in new_messages:
            if message.get("role") == "system":
                continue
            entry = {**message, "timestamp": timestamp}
            if (
                not user_message_id_attached
                and message.get("role") == "user"
                and event.message_id
                and "message_id" not in entry
            ):
                entry["message_id"] = str(event.message_id)
                user_message_id_attached = True
            writes.append((entry, agent_persisted))
        return writes


def _user_entry(message: str, message_id: Any, timestamp: str) -> dict[str, Any]:
    entry = {"role": "user", "content": message, "timestamp": timestamp}
    if message_id:
        entry["message_id"] = str(message_id)
    return entry


def agent_turn_persistence_for(runner) -> GatewayAgentTurnPersistenceService:
    service = getattr(runner, "agent_turn_persistence", None)
    if isinstance(service, GatewayAgentTurnPersistenceService):
        return service
    service = GatewayAgentTurnPersistenceService(runner)
    runner.agent_turn_persistence = service
    return service
