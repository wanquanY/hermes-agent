"""Runtime bridge from provider/tool events to the verification application seam."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

from agent.tool_dispatch_helpers import _extract_file_mutation_targets
from agent.tool_result_classification import file_mutation_result_landed
from hermes_agent.domain.verification import VerificationRequirement


logger = logging.getLogger(__name__)

_NON_MESSAGING_SURFACES = frozenset(
    {
        "",
        "api_server",
        "cli",
        "codex",
        "desktop",
        "gateway",
        "local",
        "msgraph_webhook",
        "tool",
        "tui",
        "webhook",
    }
)


def verification_completion_guard_enabled(agent: Any) -> bool:
    """Resolve the configured guard without leaking it onto chat surfaces."""
    configured = getattr(agent, "verification_completion_guard", "auto")
    if isinstance(configured, bool):
        return configured
    token = str(configured or "auto").strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    identities = (
        getattr(agent, "platform", None),
        os.getenv("HERMES_PLATFORM"),
        os.getenv("HERMES_SESSION_PLATFORM"),
        os.getenv("HERMES_SESSION_SOURCE"),
    )
    return all(
        not (normalized := str(identity or "").strip().lower())
        or normalized in _NON_MESSAGING_SURFACES
        for identity in identities
    )


def hold_verification_stream(agent: Any) -> None:
    """Buffer visible answer deltas while an edit generation is unverified."""
    if not bool(getattr(agent, "_verification_stream_hold", False)):
        agent._verification_stream_buffer = ""
    agent._verification_stream_hold = True


def release_verification_stream(agent: Any, *, deliver: bool) -> str:
    """Release or discard buffered deltas and clear the hold atomically."""
    text = str(getattr(agent, "_verification_stream_buffer", "") or "")
    agent._verification_stream_buffer = ""
    agent._verification_stream_hold = False
    if not deliver or not text:
        return text
    callbacks = getattr(agent, "_stream_delta_callbacks", lambda: [])()
    delivered = False
    for callback in callbacks:
        try:
            callback(text)
            delivered = True
        except Exception:
            logger.debug("verification buffered stream callback failed", exc_info=True)
    if delivered:
        recorder = getattr(agent, "_record_streamed_assistant_text", None)
        if callable(recorder):
            recorder(text)
    return text


def verification_scope_id(agent: Any) -> str:
    visible = getattr(agent, "_visible_transcript_session_id", None)
    if callable(visible):
        try:
            value = str(visible() or "").strip()
            if value:
                return value
        except Exception:
            logger.debug("verification visible-session resolution failed", exc_info=True)
    for name in ("memory_session_id", "_gateway_session_key", "session_id"):
        value = str(getattr(agent, name, "") or "").strip()
        if value:
            return value
    return ""


def verification_cwd(agent: Any, args: Mapping[str, Any] | None = None) -> str:
    values = args or {}
    raw = (
        values.get("workdir")
        or values.get("cwd")
        or getattr(agent, "session_cwd", None)
        or os.getcwd()
    )
    return str(Path(str(raw)).expanduser().resolve(strict=False))


def _service(agent: Any) -> Any | None:
    db = getattr(agent, "_session_db", None)
    if db is None:
        return None
    try:
        return getattr(db, "verification", None)
    except Exception:
        return None


def _terminal_payload(result: Any) -> dict[str, Any] | None:
    if isinstance(result, dict):
        return result
    if not isinstance(result, str):
        return None
    stripped = result.lstrip()
    try:
        value, _end = json.JSONDecoder().raw_decode(stripped)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def record_tool_verification(
    agent: Any,
    tool_name: str,
    args: Mapping[str, Any],
    result: Any,
    *,
    is_error: bool,
) -> None:
    """Record a Hermes-native tool result without leaking persistence policy."""
    service = _service(agent)
    scope_id = verification_scope_id(agent)
    if service is None or not scope_id:
        return
    cwd = verification_cwd(agent, args)
    try:
        if file_mutation_result_landed(tool_name, result):
            paths = _extract_file_mutation_targets(tool_name, dict(args))
            if paths:
                service.mark_edited(scope_id, cwd, paths)
                hold_verification_stream(agent)
            return
        if tool_name not in {"terminal", "shell"}:
            return
        command = str(args.get("command") or args.get("cmd") or "").strip()
        if not command:
            return
        payload = _terminal_payload(result)
        if payload is None:
            return
        raw_exit = payload.get("exit_code", payload.get("exitCode"))
        if raw_exit is None:
            return
        output = payload.get("output", payload.get("aggregatedOutput", ""))
        service.record_terminal(
            scope_id,
            command=command,
            cwd=cwd,
            exit_code=int(raw_exit),
            output=str(output or ""),
        )
    except Exception:
        # Verification evidence is a correctness guard, but an unavailable
        # state backend must not turn a completed tool call into a fake tool
        # failure. The completion policy will remain unverified/fail closed.
        logger.warning("verification tool-result recording failed", exc_info=True)


def record_codex_item_verification(agent: Any, item: Mapping[str, Any]) -> None:
    """Project completed app-server items into the same application service."""
    service = _service(agent)
    scope_id = verification_scope_id(agent)
    if service is None or not scope_id:
        return
    item_type = str(item.get("type") or "")
    try:
        if item_type == "fileChange":
            status = str(item.get("status") or "").lower()
            if status not in {"applied", "completed"}:
                return
            paths = []
            for change in item.get("changes") or ():
                if isinstance(change, Mapping):
                    path = change.get("path") or change.get("filename")
                    if path:
                        paths.append(str(path))
            if paths:
                service.mark_edited(
                    scope_id,
                    verification_cwd(agent, item),
                    paths,
                )
                hold_verification_stream(agent)
            return
        if item_type != "commandExecution":
            return
        command = item.get("command") or ""
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        exit_code = item.get("exitCode")
        if exit_code is None:
            return
        service.record_terminal(
            scope_id,
            command=str(command),
            cwd=verification_cwd(agent, item),
            exit_code=int(exit_code),
            output=str(item.get("aggregatedOutput") or ""),
        )
    except Exception:
        logger.warning("verification Codex item recording failed", exc_info=True)


def completion_requirement_for_agent(
    agent: Any,
    *,
    attempt: int,
) -> VerificationRequirement | None:
    if not verification_completion_guard_enabled(agent):
        return None
    service = _service(agent)
    scope_id = verification_scope_id(agent)
    if service is None or not scope_id:
        return None
    max_attempts = max(0, int(getattr(agent, "verification_max_attempts", 1) or 0))
    try:
        requirement = service.completion_requirement(
            scope_id,
            verification_cwd(agent),
            attempt=max(0, int(attempt)),
            max_attempts=max_attempts,
        )
    except Exception:
        logger.warning("verification completion decision failed", exc_info=True)
        return None
    if requirement is None:
        return None
    if isinstance(requirement, VerificationRequirement):
        return requirement
    if isinstance(requirement, Mapping):
        try:
            return VerificationRequirement(
                scope_id=str(requirement.get("scope_id") or ""),
                workspace_root=str(requirement.get("workspace_root") or ""),
                status=str(requirement.get("status") or "unverified"),
                edit_generation=int(requirement.get("edit_generation") or 0),
                changed_paths=tuple(requirement.get("changed_paths") or ()),
                verify_commands=tuple(requirement.get("verify_commands") or ()),
                attempt=int(requirement.get("attempt") or 1),
                max_attempts=int(requirement.get("max_attempts") or max_attempts or 1),
            )
        except (TypeError, ValueError):
            logger.warning("invalid verification requirement returned by state proxy")
    return None


__all__ = [
    "completion_requirement_for_agent",
    "hold_verification_stream",
    "record_codex_item_verification",
    "record_tool_verification",
    "release_verification_stream",
    "verification_completion_guard_enabled",
    "verification_cwd",
    "verification_scope_id",
]
