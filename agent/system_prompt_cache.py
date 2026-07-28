"""Cache identity for system prompts whose content depends on runtime state.

Team conversations share one transcript session while different participants
and runtime modes execute under isolated scopes.  A scope alone is not a
complete prompt identity: the rendered prompt also contains model- and
tool-aware guidance.  This module keeps that dependency contract in one place
so prompt readers and writers cannot accidentally address different snapshots.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any


_PROMPT_CACHE_CONTRACT_VERSION = 1


def _execution_scope_identity(
    agent: Any,
    *,
    session_id: str | None = None,
    contexts: Iterable[Any] = (),
) -> tuple[str, Any | None]:
    conversation_identity = str(
        session_id or getattr(agent, "session_id", "") or ""
    ).strip()
    candidates = (
        *tuple(contexts),
        getattr(agent, "run_context", None),
        getattr(agent, "_run_context", None),
    )
    for context in candidates:
        conversation_session_id = str(
            getattr(context, "conversation_session_id", "") or ""
        ).strip()
        execution_scope_key = str(
            getattr(context, "execution_scope_key", "") or ""
        ).strip()
        if (
            conversation_session_id
            and execution_scope_key
            and conversation_session_id == conversation_identity
            and execution_scope_key != conversation_identity
        ):
            return execution_scope_key, context
    return "", None


def _json_safe_prompt_setting(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple, set)):
        return sorted(str(item) for item in value)
    return str(value)


def system_prompt_cache_scope_key(
    agent: Any,
    *,
    session_id: str | None = None,
    contexts: Iterable[Any] = (),
) -> str:
    """Return the persisted cache key for a scoped system-prompt variant.

    Ordinary one-agent sessions continue to use ``sessions.system_prompt``.
    Multi-participant executions use a scoped snapshot whose key includes the
    prompt-shaping participant, profile, model, and tool-surface identity.
    Alternating a
    Leader between an interactive tool surface and a terminal tool-free report
    therefore cannot restore the other mode's prompt.
    """
    execution_scope_key, run_context = _execution_scope_identity(
        agent,
        session_id=session_id,
        contexts=contexts,
    )
    if not execution_scope_key:
        return ""

    contract = {
        "version": _PROMPT_CACHE_CONTRACT_VERSION,
        "model": str(getattr(agent, "model", "") or ""),
        "provider": str(getattr(agent, "provider", "") or ""),
        "platform": str(getattr(agent, "platform", "") or ""),
        "valid_tool_names": sorted(
            str(name)
            for name in (getattr(agent, "valid_tool_names", None) or set())
        ),
        "tool_use_enforcement": _json_safe_prompt_setting(
            getattr(agent, "_tool_use_enforcement", None)
        ),
        "skip_context_files": bool(getattr(agent, "skip_context_files", False)),
        "load_soul_identity": bool(getattr(agent, "load_soul_identity", False)),
        "profile_id": str(getattr(run_context, "profile_id", "") or ""),
        "profile_version_id": str(
            getattr(run_context, "profile_version_id", "") or ""
        ),
        "execution_home": str(
            getattr(run_context, "execution_home", "") or ""
        ),
        "kanban_worker_guidance": str(
            getattr(agent, "_kanban_worker_guidance", "") or ""
        ),
    }
    encoded = json.dumps(
        contract,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()[:20]
    return (
        f"{execution_scope_key}:prompt-v{_PROMPT_CACHE_CONTRACT_VERSION}:{digest}"
    )
