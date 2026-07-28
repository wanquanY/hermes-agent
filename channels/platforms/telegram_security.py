"""Security helpers shared by Telegram transport modules."""

from __future__ import annotations


def redact_telegram_error(error: object) -> str:
    """Return a log-safe Telegram error string with credentials removed."""
    text = "" if error is None else str(error)
    if not text:
        return text
    try:
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(text, force=True)
    except Exception:
        return "<telegram error redacted>"


# Preserve the upstream helper name for compatibility with plugins and tests.
_redact_telegram_error_text = redact_telegram_error
