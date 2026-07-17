"""Prepare agent history and user input for a gateway turn."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PreparedAgentInput:
    message: Any
    agent_history: list[dict[str, Any]]
    history_media_paths: set[str]


class AgentInputPreparation:
    def __init__(
        self,
        *,
        runner,
        session_key: str | None,
        history: list[dict[str, Any]],
        build_replay_entry: Callable[[str, str, dict[str, Any]], dict[str, Any]],
        collect_history_media_paths: Callable[[list[dict[str, Any]]], set[str]],
        last_transcript_timestamp: Callable[[list[dict[str, Any]]], Any],
        is_fresh_gateway_interruption: Callable[..., bool],
        auto_continue_freshness_window: Callable[[], float],
        consume_native_image_paths: Callable[[str | None], list[str]],
    ) -> None:
        self._runner = runner
        self._session_key = session_key
        self._history = history
        self._build_replay_entry = build_replay_entry
        self._collect_history_media_paths = collect_history_media_paths
        self._last_transcript_timestamp = last_transcript_timestamp
        self._is_fresh_gateway_interruption = is_fresh_gateway_interruption
        self._auto_continue_freshness_window = auto_continue_freshness_window
        self._consume_native_image_paths = consume_native_image_paths

    def prepare(self, message: str) -> PreparedAgentInput:
        agent_history = self._convert_history()
        history_media_paths = self._collect_history_media_paths(agent_history)
        prepared_message = self._apply_pending_notes(message, agent_history)
        run_message = self._build_run_message(prepared_message)
        return PreparedAgentInput(
            message=run_message,
            agent_history=agent_history,
            history_media_paths=history_media_paths,
        )

    def _convert_history(self) -> list[dict[str, Any]]:
        agent_history: list[dict[str, Any]] = []
        for msg in self._history:
            role = msg.get("role")
            if not role or role in {"session_meta", "system"}:
                continue

            has_tool_calls = "tool_calls" in msg
            has_tool_call_id = "tool_call_id" in msg
            is_tool_message = role == "tool"
            if has_tool_calls or has_tool_call_id or is_tool_message:
                agent_history.append({k: v for k, v in msg.items() if k != "timestamp"})
                continue

            content = msg.get("content")
            if not content:
                continue
            if msg.get("mirror"):
                mirror_src = msg.get("mirror_source", "another session")
                content = f"[Delivered from {mirror_src}] {content}"
            agent_history.append(self._build_replay_entry(role, content, msg))
        return agent_history

    def _apply_pending_notes(
        self,
        message: str,
        agent_history: list[dict[str, Any]],
    ) -> str:
        message = self._prepend_pending_model_note(message)
        message = self._prepend_auto_continue_note(message, agent_history)
        return self._prepend_pending_skills_reload_note(message)

    def _prepend_pending_model_note(self, message: str) -> str:
        pending_notes = getattr(self._runner, "_pending_model_notes", {})
        note = pending_notes.pop(self._session_key, None) if self._session_key else None
        return f"{note}\n\n{message}" if note else message

    def _prepend_pending_skills_reload_note(self, message: str) -> str:
        pending_notes = getattr(self._runner, "_pending_skills_reload_notes", None)
        if not (pending_notes and self._session_key and self._session_key in pending_notes):
            return message
        note = pending_notes.pop(self._session_key, None)
        return f"{note}\n\n{message}" if note else message

    def _prepend_auto_continue_note(
        self,
        message: str,
        agent_history: list[dict[str, Any]],
    ) -> str:
        freshness_window = self._auto_continue_freshness_window()
        interruption_is_fresh = self._is_fresh_gateway_interruption(
            self._last_transcript_timestamp(self._history),
            window_secs=freshness_window,
        )
        resume_entry = None
        if self._session_key:
            session_store = getattr(self._runner, "session_store", None)
            get_entry = getattr(session_store, "get_entry", None)
            if callable(get_entry):
                resume_entry = get_entry(self._session_key)
        resume_mark_is_fresh = bool(
            resume_entry is not None
            and getattr(resume_entry, "resume_pending", False)
            and self._is_fresh_gateway_interruption(
                getattr(resume_entry, "last_resume_marked_at", None),
                window_secs=freshness_window,
            )
        )
        is_resume_pending = bool(
            resume_entry is not None
            and getattr(resume_entry, "resume_pending", False)
            and (interruption_is_fresh or resume_mark_is_fresh)
        )
        has_fresh_tool_tail = bool(
            agent_history
            and agent_history[-1].get("role") == "tool"
            and interruption_is_fresh
        )

        if is_resume_pending:
            reason = getattr(resume_entry, "resume_reason", None) or "restart_timeout"
            reason_phrase = (
                "a gateway restart"
                if reason == "restart_timeout"
                else "a gateway shutdown"
                if reason == "shutdown_timeout"
                else "a gateway interruption"
            )
            return (
                f"[System note: Your previous turn in this session was interrupted "
                f"by {reason_phrase}. The conversation history below is intact. "
                f"If it contains unfinished tool result(s), process them first and "
                f"summarize what was accomplished, then address the user's new "
                f"message below.]\n\n"
                + message
            )
        if has_fresh_tool_tail:
            return (
                "[System note: Your previous turn was interrupted before you could "
                "process the last tool result(s). The conversation history contains "
                "tool outputs you haven't responded to yet. Please finish processing "
                "those results and summarize what was accomplished, then address the "
                "user's new message below.]\n\n"
                + message
            )
        if (
            not str(message or "").strip()
            and resume_entry is not None
            and getattr(resume_entry, "resume_pending", False)
        ):
            return (
                "[System note: The previous turn in this session was interrupted "
                "by a gateway restart. Review the intact conversation history, "
                "finish any unfinished work, summarize what was accomplished, "
                "then wait for the user's next message.]"
            )
        return message

    def _build_run_message(self, message: str) -> Any:
        native_images = self._consume_native_image_paths(self._session_key)
        if not native_images:
            return message
        try:
            from agent.image_routing import build_native_content_parts

            parts, skipped = build_native_content_parts(message, native_images)
            if skipped:
                logger.warning(
                    "Native image attachment: skipped %d unreadable path(s): %s",
                    len(skipped),
                    skipped,
                )
            if any(part.get("type") == "image_url" for part in parts):
                return parts
        except Exception as exc:
            logger.warning("Native image attachment failed, falling back to text: %s", exc)
        return message


def agent_input_preparation_for(**kwargs) -> AgentInputPreparation:
    return AgentInputPreparation(**kwargs)
