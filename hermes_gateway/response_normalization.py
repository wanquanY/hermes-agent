"""Gateway response normalization rules."""

from __future__ import annotations


DOVIE_RUNTIME_AUTH_FAILURE_MESSAGE = (
    "⚠️ Dovie runtime 登录凭证已过期，正在刷新本地运行时。请稍后再试一次。"
)


def is_dovie_runtime_auth_failure(value: object) -> bool:
    text = str(value or "").lower()
    return (
        "runtime token has expired or was revoked" in text
        or "runtime_token_error" in text
        or "dovie_auth_required" in text
        or "runtime_scope_forbidden" in text
    )


def normalize_empty_agent_response(
    agent_result: dict,
    response: str,
    *,
    history_len: int = 0,
) -> str:
    """Normalize empty/None agent responses into user-facing messages."""
    if response:
        if is_dovie_runtime_auth_failure(response):
            return DOVIE_RUNTIME_AUTH_FAILURE_MESSAGE
        return response

    if agent_result.get("failed"):
        error_detail = agent_result.get("error", "unknown error")
        if is_dovie_runtime_auth_failure(error_detail):
            return DOVIE_RUNTIME_AUTH_FAILURE_MESSAGE
        error_str = str(error_detail).lower()
        is_context_failure = any(
            p in error_str
            for p in ("context", "token", "too large", "too long", "exceed", "payload")
        ) or ("400" in error_str and history_len > 50)
        if is_context_failure:
            return (
                "⚠️ Session too large for the model's context window.\n"
                "Use /compact to compress the conversation, or "
                "/reset to start fresh."
            )
        return (
            f"The request failed: {str(error_detail)[:300]}\n"
            "Try again or use /reset to start a fresh session."
        )

    api_calls = int(agent_result.get("api_calls", 0) or 0)
    if api_calls > 0 and not agent_result.get("interrupted"):
        if agent_result.get("partial"):
            err = agent_result.get("error", "processing incomplete")
            return f"⚠️ Processing stopped: {str(err)[:200]}. Try again."
        return (
            "⚠️ Processing completed but no response was generated. "
            "This may be a transient error — try sending your message again."
        )

    return response
