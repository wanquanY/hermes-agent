"""Application coordinator for Codex app-server native compaction."""

from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__)


def codex_app_server_compaction_mode(agent: Any) -> str:
    value = str(
        getattr(agent, "codex_app_server_auto_compaction", "native") or "native"
    ).strip().lower()
    return value if value in {"native", "hermes", "off"} else "native"


def should_skip_hermes_preflight_compaction(agent: Any) -> bool:
    return (
        getattr(agent, "api_mode", None) == "codex_app_server"
        and codex_app_server_compaction_mode(agent) in {"native", "off"}
    )


def _existing_system_prompt(agent: Any, system_message: str | None) -> str:
    prompt = getattr(agent, "_cached_system_prompt", None)
    if prompt:
        return prompt
    return agent._build_system_prompt(system_message)


def compact_codex_app_server_context(
    agent: Any,
    messages: list,
    system_message: str | None,
    *,
    approx_tokens: int | None = None,
    task_id: str = "default",
) -> tuple[list, str]:
    """Compact the Codex-owned thread without rewriting Hermes transcript."""
    session = getattr(agent, "_codex_session", None)
    if session is None:
        logger.warning(
            "codex app-server compaction skipped: no active session (session=%s)",
            getattr(agent, "session_id", None) or "none",
        )
        return messages, _existing_system_prompt(agent, system_message)

    result = session.compact_thread()
    if getattr(result, "should_retire", False):
        try:
            session.close()
        except Exception:
            logger.debug("codex compaction session retirement failed", exc_info=True)
        agent._codex_session = None

    interrupted = bool(getattr(result, "interrupted", False))
    error = str(getattr(result, "error", None) or "").strip()
    compacted = bool(getattr(result, "compacted", False))
    if interrupted or error or not compacted:
        detail = error or (
            "compact turn interrupted" if interrupted else "missing contextCompaction boundary"
        )
        try:
            agent._emit_warning(f"⚠ Codex app-server compaction failed: {detail}")
        except Exception:
            pass
        return messages, _existing_system_prompt(agent, system_message)

    from agent.codex_runtime import (
        _record_codex_app_server_compaction,
        _record_codex_app_server_usage,
    )

    _record_codex_app_server_compaction(
        agent,
        result,
        approx_tokens=approx_tokens,
    )
    _record_codex_app_server_usage(agent, result)

    try:
        from tools.file_tools import reset_file_dedup

        reset_file_dedup(task_id)
    except Exception:
        logger.debug("file dedup reset after codex compaction failed", exc_info=True)

    logger.info(
        "codex app-server compaction completed: session=%s thread=%s turn=%s",
        getattr(agent, "session_id", None) or "none",
        getattr(result, "thread_id", None) or "",
        getattr(result, "turn_id", None) or "",
    )
    return messages, _existing_system_prompt(agent, system_message)


__all__ = [
    "codex_app_server_compaction_mode",
    "compact_codex_app_server_context",
    "should_skip_hermes_preflight_compaction",
]
