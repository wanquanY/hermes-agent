from __future__ import annotations

import re
from typing import Any


_RETRY_RE = re.compile(
    r"Retrying in\s+(?P<delay>\d+(?:\.\d+)?)s\s+\(attempt\s+(?P<attempt>\d+)/(?P<max>\d+)\)",
    re.IGNORECASE,
)
_RATE_LIMIT_RE = re.compile(
    r"Rate limited\.\s+Waiting\s+(?P<delay>\d+(?:\.\d+)?)s\s+\(attempt\s+(?P<attempt>\d+)/(?P<max>\d+)\)",
    re.IGNORECASE,
)
_STREAM_RETRY_RE = re.compile(
    r"retry\s+(?P<attempt>\d+)/(?P<max>\d+)",
    re.IGNORECASE,
)


def classify_status_update(kind: str, text: str) -> dict[str, Any]:
    """Return a structured gateway payload for user-visible agent status.

    AIAgent emits human-readable lifecycle lines for retry, fallback and
    transport recovery. The gateway keeps the raw text for compatibility, but
    annotates transient network/provider states so clients can render a stable
    progress line instead of hiding important retry behavior in terminal logs.
    """
    body = str(text or "").strip()
    payload: dict[str, Any] = {"kind": kind, "text": body}
    lower = body.lower()

    retry = _RETRY_RE.search(body)
    if retry:
        delay = float(retry.group("delay"))
        attempt = int(retry.group("attempt"))
        max_attempts = int(retry.group("max"))
        payload.update({
            "category": "connection",
            "state": "retrying",
            "delay_s": delay,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "display_text": f"连接暂时中断，正在重连（第 {attempt}/{max_attempts} 次，约 {delay:g} 秒后）",
        })
        return payload

    rate_limit = _RATE_LIMIT_RE.search(body)
    if rate_limit:
        delay = float(rate_limit.group("delay"))
        attempt = int(rate_limit.group("attempt"))
        max_attempts = int(rate_limit.group("max"))
        payload.update({
            "category": "connection",
            "state": "waiting",
            "delay_s": delay,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "display_text": f"请求被限流，{delay:g} 秒后重试（第 {attempt}/{max_attempts} 次）",
        })
        return payload

    if "stream" in lower and "reconnecting" in lower:
        stream_retry = _STREAM_RETRY_RE.search(body)
        if stream_retry:
            attempt = int(stream_retry.group("attempt"))
            max_attempts = int(stream_retry.group("max"))
            payload.update({
                "attempt": attempt,
                "max_attempts": max_attempts,
                "display_text": f"流式连接中断，正在重连（第 {attempt}/{max_attempts} 次）",
            })
        else:
            payload["display_text"] = "流式连接中断，正在重连"
        payload.update({"category": "connection", "state": "retrying"})
        return payload

    if "max retries" in lower and "trying fallback" in lower:
        payload.update({
            "category": "connection",
            "state": "fallback",
            "display_text": "重试已用尽，正在尝试备用通道",
        })
        return payload

    if "switching to fallback" in lower:
        payload.update({
            "category": "connection",
            "state": "fallback",
            "display_text": "当前通道不可用，正在切换备用通道",
        })
        return payload

    if "api failed after" in lower or "rate limited after" in lower:
        payload.update({
            "category": "connection",
            "state": "failed",
            "display_text": body,
        })
        return payload

    return payload
