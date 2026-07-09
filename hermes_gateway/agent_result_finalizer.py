"""Normalize and finalize gateway agent run results."""

from __future__ import annotations

import logging
import re
from typing import Any

from hermes_gateway.config import Platform

logger = logging.getLogger(__name__)

_TOOL_MEDIA_RE = re.compile(
    r"MEDIA:((?:/|~/)\S+\.(?:png|jpe?g|gif|webp|"
    r"mp4|mov|avi|mkv|webm|ogg|opus|mp3|wav|m4a|"
    r"flac|epub|pdf|zip|rar|7z|docx?|xlsx?|pptx?|"
    r"txt|csv|apk|ipa))",
    re.IGNORECASE,
)


class AgentResultFinalizer:
    def __init__(
        self,
        *,
        session_store,
        session_db,
        source,
        session_id: str,
        session_key: str | None,
        tools: list[Any] | None,
        history_offset: int,
        history_media_paths: set[str],
    ) -> None:
        self._session_store = session_store
        self._session_db = session_db
        self._source = source
        self._session_id = session_id
        self._session_key = session_key
        self._tools = tools or []
        self._history_offset = history_offset
        self._history_media_paths = history_media_paths

    def finalize(self, result: dict[str, Any], agent) -> dict[str, Any]:
        final_response = result.get("final_response")
        token_summary = self._token_summary(agent)
        if not final_response:
            return self._empty_response(result, token_summary)

        final_response = self._append_media_tags(final_response, result)
        effective_session_id, session_was_split = self._sync_split_session(agent)
        effective_history_offset = 0 if session_was_split else self._history_offset
        return {
            "final_response": final_response,
            "last_reasoning": result.get("last_reasoning"),
            "messages": result.get("messages", []),
            "api_calls": result.get("api_calls", 0),
            "completed": result.get("completed"),
            "interrupted": result.get("interrupted", False),
            "partial": result.get("partial", False),
            "error": result.get("error"),
            "interrupt_message": result.get("interrupt_message"),
            "tools": self._tools,
            "history_offset": effective_history_offset,
            "last_prompt_tokens": token_summary["last_prompt_tokens"],
            "input_tokens": token_summary["input_tokens"],
            "output_tokens": token_summary["output_tokens"],
            "model": token_summary["model"],
            "context_length": token_summary["context_length"],
            "session_id": effective_session_id,
            "response_previewed": result.get("response_previewed", False),
        }

    def _empty_response(
        self,
        result: dict[str, Any],
        token_summary: dict[str, Any],
    ) -> dict[str, Any]:
        error_msg = f"⚠️ {result['error']}" if result.get("error") else ""
        return {
            "final_response": error_msg,
            "messages": result.get("messages", []),
            "api_calls": result.get("api_calls", 0),
            "failed": result.get("failed", False),
            "partial": result.get("partial", False),
            "completed": result.get("completed"),
            "interrupted": result.get("interrupted", False),
            "interrupt_message": result.get("interrupt_message"),
            "error": result.get("error"),
            "compression_exhausted": result.get("compression_exhausted", False),
            "tools": self._tools,
            "history_offset": self._history_offset,
            "last_prompt_tokens": token_summary["last_prompt_tokens"],
            "input_tokens": token_summary["input_tokens"],
            "output_tokens": token_summary["output_tokens"],
            "model": token_summary["model"],
            "context_length": token_summary["context_length"],
        }

    def _token_summary(self, agent) -> dict[str, Any]:
        prompt_tokens = 0
        input_tokens = 0
        output_tokens = 0
        context_length = 0
        if agent and hasattr(agent, "context_compressor"):
            prompt_tokens = getattr(agent.context_compressor, "last_prompt_tokens", 0)
            input_tokens = getattr(agent, "session_prompt_tokens", 0)
            output_tokens = getattr(agent, "session_completion_tokens", 0)
            context_length = getattr(agent.context_compressor, "context_length", 0) or 0
        return {
            "last_prompt_tokens": prompt_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "model": getattr(agent, "model", None) if agent else None,
            "context_length": context_length,
        }

    def _append_media_tags(self, final_response: str, result: dict[str, Any]) -> str:
        if "MEDIA:" in final_response:
            return final_response
        media_tags: list[str] = []
        has_voice_directive = False
        for msg in result.get("messages", []):
            if msg.get("role") not in {"tool", "function"}:
                continue
            content = msg.get("content", "")
            if "MEDIA:" in content:
                for match in _TOOL_MEDIA_RE.finditer(content):
                    path = match.group(1).strip().rstrip('",}')
                    if path and path not in self._history_media_paths:
                        media_tags.append(f"MEDIA:{path}")
                if "[[audio_as_voice]]" in content:
                    has_voice_directive = True
        if not media_tags:
            return final_response
        seen: set[str] = set()
        unique_tags: list[str] = []
        for tag in media_tags:
            if tag not in seen:
                seen.add(tag)
                unique_tags.append(tag)
        if has_voice_directive:
            unique_tags.insert(0, "[[audio_as_voice]]")
        return final_response + "\n" + "\n".join(unique_tags)

    def _sync_split_session(self, agent) -> tuple[str, bool]:
        if not agent:
            return self._session_id, False
        effective_session_id = getattr(agent, "session_id", self._session_id)
        session_was_split = bool(
            self._session_key
            and hasattr(agent, "session_id")
            and agent.session_id != self._session_id
        )
        if not session_was_split:
            return effective_session_id, False

        logger.info(
            "Session split detected: %s → %s (compression)",
            self._session_id,
            agent.session_id,
        )
        update_session_id = getattr(self._session_store, "update_entry_session_id", None)
        if callable(update_session_id):
            update_session_id(self._session_key, agent.session_id)
        self._restore_telegram_thread_id_after_split(agent.session_id)
        return effective_session_id, True

    def _restore_telegram_thread_id_after_split(self, session_id: str) -> None:
        if not (
            getattr(self._source, "platform", None) == Platform.TELEGRAM
            and getattr(self._source, "chat_type", None) == "dm"
            and getattr(self._source, "thread_id", None) is None
            and self._session_db is not None
        ):
            return
        try:
            binding = self._session_db.get_telegram_topic_binding_by_session(
                session_id=session_id,
            )
            if binding and binding.get("thread_id"):
                self._source.thread_id = str(binding["thread_id"])
                logger.debug(
                    "Restored source.thread_id=%s from binding after session split %s → %s",
                    self._source.thread_id,
                    self._session_id,
                    session_id,
                )
        except Exception:
            logger.debug(
                "Failed to restore thread_id from binding after session split",
                exc_info=True,
            )


def agent_result_finalizer_for(**kwargs) -> AgentResultFinalizer:
    return AgentResultFinalizer(**kwargs)
