"""Unified approval orchestration for commands and arbitrary tool policies.

``tools.approval`` owns detection, session state, persistence and blocking
primitives. This module owns the one decision workflow shared by every tool
surface: display redaction, responder selection, denial shaping and scope
persistence.
"""

from __future__ import annotations

import hashlib
import logging

from utils import env_var_enabled

logger = logging.getLogger(__name__)


def _redact_user_visible(value: object) -> str:
    """Return a display-only redacted value without risking raw fallback."""
    text = str(value or "")
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text)
    except Exception as exc:  # pragma: no cover - defensive fail-closed seam
        logger.error("Approval display redaction failed: %s", exc, exc_info=True)
        return "[approval details unavailable: redaction failed]"


def _denied_result(
    *,
    subject: str,
    pattern_key: str,
    description: str,
    resolved: bool,
    reason: str | None,
) -> dict:
    """Build one denial/timeout contract for every approval surface."""
    outcome = "denied" if resolved else "timeout"
    decision_text = "denied by user" if resolved else "timed out without user response"
    reason_suffix = f" User reason: {reason}" if reason else ""
    silence_suffix = " Silence is not consent." if not resolved else ""
    result = {
        "approved": False,
        "message": (
            f"BLOCKED: {subject} was {decision_text}. The user has NOT consented "
            "to this action. Do NOT retry, rephrase, use a different command, "
            f"or attempt the same outcome through another tool.{reason_suffix}"
            f"{silence_suffix}"
        ),
        "pattern_key": pattern_key,
        "description": description,
        "outcome": outcome,
        "user_consent": False,
    }
    if reason:
        result["denial_reason"] = reason
    return result


def run_approval_gate(
    *,
    pattern_keys: list[str],
    description: str,
    display_target: str,
    subject: str,
    approval_callback=None,
    permanent_keys: set[str] | None = None,
    persist_decision: bool = True,
    timeout_seconds: int | None = None,
    surface: str = "gateway",
    fail_closed_when_no_human: bool = False,
    no_human_block_message: str = "",
) -> dict:
    """Run the single human approval gate for commands and arbitrary tools.

    Detection stays with the caller; all state lookup, display redaction,
    transport selection, waiting, denial shaping and persistence live here.
    """
    from tools import approval as state

    keys = list(dict.fromkeys(key for key in pattern_keys if key))
    if not keys:
        return {
            "approved": False,
            "message": "BLOCKED: approval policy produced no stable rule key.",
            "outcome": "policy_error",
            "user_consent": False,
        }
    permanent = set(keys) if permanent_keys is None else set(permanent_keys)
    session_key = state.get_current_session_key()

    if all(state.is_approved(session_key, key) for key in keys):
        return {"approved": True, "message": None, "cached_approval": True}

    if (
        state.is_process_yolo_enabled()
        or state.is_current_session_yolo_enabled()
        or state.get_approval_mode() == "off"
    ):
        return {"approved": True, "message": None, "policy_bypass": True}

    is_cli = env_var_enabled("HERMES_INTERACTIVE")
    is_gateway = state.is_gateway_approval_context()
    is_ask = env_var_enabled("HERMES_EXEC_ASK")
    notify_cb = state.get_gateway_notify_callback(session_key)

    display_description = _redact_user_visible(description)
    safe_target = _redact_user_visible(display_target)
    primary_key = keys[0]
    allow_permanent = all(key in permanent for key in keys)

    if is_gateway or is_ask:
        if notify_cb is None:
            if fail_closed_when_no_human:
                return {
                    "approved": False,
                    "message": no_human_block_message or (
                        f"BLOCKED: {subject} requires human approval, but no "
                        "approval responder is attached."
                    ),
                    "pattern_key": primary_key,
                    "description": display_description,
                    "outcome": "no_human_responder",
                    "user_consent": False,
                }
            approval_data = {
                "command": safe_target,
                "pattern_key": primary_key,
                "pattern_keys": keys,
                "description": display_description,
                "allow_permanent": allow_permanent,
            }
            state.submit_pending(session_key, approval_data)
            return {
                "approved": False,
                "pattern_key": primary_key,
                "status": "pending_approval",
                "approval_pending": True,
                "command": safe_target,
                "description": display_description,
                "message": (
                    f"⚠️ {display_description}. Asking the user for approval.\n\n"
                    f"**Target:**\n```\n{safe_target}\n```"
                ),
            }

        decision = state.await_gateway_decision(
            session_key,
            notify_cb,
            {
                "command": safe_target,
                "pattern_key": primary_key,
                "pattern_keys": keys,
                "description": display_description,
                "allow_permanent": allow_permanent,
            },
            surface=surface,
            timeout_seconds=timeout_seconds,
        )
        if decision.get("notify_failed"):
            return {
                "approved": False,
                "message": (
                    f"BLOCKED: failed to send the {subject} approval request. "
                    "The user has NOT consented. Do NOT retry."
                ),
                "pattern_key": primary_key,
                "description": display_description,
                "outcome": "notify_failed",
                "user_consent": False,
            }
        resolved = bool(decision.get("resolved"))
        choice = decision.get("choice")
        if not resolved or choice not in {"once", "session", "always"}:
            return _denied_result(
                subject=subject,
                pattern_key=primary_key,
                description=display_description,
                resolved=resolved,
                reason=decision.get("reason"),
            )
        if persist_decision:
            state.persist_approval_choice(session_key, choice, keys, permanent)
        return {
            "approved": True,
            "message": None,
            "user_approved": True,
            "description": display_description,
        }

    if not is_cli:
        if fail_closed_when_no_human:
            return {
                "approved": False,
                "message": no_human_block_message or (
                    f"BLOCKED: {subject} requires human approval in an "
                    "unattended execution context."
                ),
                "pattern_key": primary_key,
                "description": display_description,
                "outcome": "no_human_responder",
                "user_consent": False,
            }
        return {"approved": True, "message": None, "unattended_policy": True}

    state.fire_approval_hook(
        "pre_approval_request",
        command=safe_target,
        description=display_description,
        pattern_key=primary_key,
        pattern_keys=keys,
        session_key=session_key,
        surface="cli",
    )
    choice = state.prompt_dangerous_approval(
        safe_target,
        display_description,
        allow_permanent=allow_permanent,
        timeout_seconds=timeout_seconds,
        approval_callback=approval_callback,
    )
    state.fire_approval_hook(
        "post_approval_response",
        command=safe_target,
        description=display_description,
        pattern_key=primary_key,
        pattern_keys=keys,
        session_key=session_key,
        surface="cli",
        choice=choice,
    )
    if choice not in {"once", "session", "always"}:
        return _denied_result(
            subject=subject,
            pattern_key=primary_key,
            description=display_description,
            resolved=True,
            reason=None,
        )
    if persist_decision:
        state.persist_approval_choice(session_key, choice, keys, permanent)
    return {
        "approved": True,
        "message": None,
        "user_approved": True,
        "description": display_description,
    }


def request_tool_approval(
    tool_name: str,
    reason: str,
    *,
    rule_key: str = "",
    approval_callback=None,
) -> dict:
    """Escalate a plugin tool directive through the shared human gate."""
    if approval_callback is None:
        try:
            from tools.terminal_tool import get_approval_callback

            approval_callback = get_approval_callback()
        except Exception:
            approval_callback = None
    normalized_tool = str(tool_name or "unknown_tool").strip()[:200] or "unknown_tool"
    description = str(reason or "Plugin policy requires approval.").strip()
    stable_rule = str(rule_key or "").strip()[:256]
    if not stable_rule:
        digest = hashlib.sha256(description.encode("utf-8")).hexdigest()[:12]
        stable_rule = f"{normalized_tool}:{digest}"
    pattern_key = f"plugin_rule:{stable_rule}"
    return run_approval_gate(
        pattern_keys=[pattern_key],
        permanent_keys={pattern_key},
        description=description,
        display_target=f"<{normalized_tool}> (plugin approval rule)",
        subject=f"tool '{normalized_tool}'",
        approval_callback=approval_callback,
        fail_closed_when_no_human=True,
        no_human_block_message=(
            f"BLOCKED: plugin policy requires human approval for tool "
            f"'{normalized_tool}', but no approval responder is attached."
        ),
    )
