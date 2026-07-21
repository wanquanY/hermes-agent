"""Pure policy helpers for browser evaluation and private-network guards."""

from __future__ import annotations

import re
from typing import Callable, Optional

from hermes_cli.config import cfg_get
from utils import is_truthy_value

_JS_URL_LITERAL_RE = re.compile(r"""https?://[^\s'"`)\]<>]+""", re.IGNORECASE)
_JS_STRING_LITERAL_RE = re.compile(
    r"""'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`""",
    re.DOTALL,
)
_RISKY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bdocument\s*\.\s*cookie\b", re.I), "document.cookie"),
    (re.compile(r"\b(?:localStorage|sessionStorage)\b", re.I), "web storage"),
    (re.compile(r"\bindexedDB\b", re.I), "IndexedDB"),
    (re.compile(r"\bcaches\s*\.\s*(?:open|match|keys)\b", re.I), "Cache Storage"),
    (re.compile(r"\bnavigator\s*\.\s*(?:clipboard|credentials|serviceWorker)\b", re.I), "navigator sensitive API"),
    (re.compile(r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource)\s*\(", re.I), "network request"),
    (re.compile(r"\bnavigator\s*\.\s*sendBeacon\s*\(", re.I), "network beacon"),
    (re.compile(r"\bdocument\s*\.\s*forms\b.*\bvalue\b", re.I | re.S), "form value extraction"),
    (re.compile(r"\bquerySelector(?:All)?\s*\([^)]*(?:input|textarea|password)[^)]*\).*\bvalue\b", re.I | re.S), "form value extraction"),
)
_SENSITIVE_TOKENS: tuple[tuple[str, str], ...] = (
    ("cookie", "document.cookie"),
    ("localStorage", "web storage"),
    ("sessionStorage", "web storage"),
    ("indexedDB", "IndexedDB"),
    ("caches", "Cache Storage"),
    ("clipboard", "navigator sensitive API"),
    ("credentials", "navigator sensitive API"),
    ("serviceWorker", "navigator sensitive API"),
    ("fetch", "network request"),
    ("XMLHttpRequest", "network request"),
    ("WebSocket", "network request"),
    ("EventSource", "network request"),
    ("sendBeacon", "network beacon"),
)


def expression_targets_private_url(
    expression: str,
    *,
    is_blocked_url: Callable[[str], bool],
) -> Optional[str]:
    """Return the first private/internal URL literal in JavaScript source."""
    if not isinstance(expression, str):
        return None
    for match in _JS_URL_LITERAL_RE.findall(expression):
        candidate = match.rstrip(".,;")
        if is_blocked_url(candidate):
            return candidate
    return None


def _decode_literal(literal: str) -> str:
    if len(literal) < 2:
        return literal
    body = literal[1:-1]
    try:
        return bytes(body, "utf-8").decode("unicode_escape")
    except Exception:
        return body


def risky_evaluate_reason(expression: str) -> Optional[str]:
    """Detect sensitive browser primitives, including quoted spellings."""
    if not expression:
        return None
    for pattern, reason in _RISKY_PATTERNS:
        if pattern.search(expression):
            return reason
    literals = [_decode_literal(match.group(0)) for match in _JS_STRING_LITERAL_RE.finditer(expression)]
    joined = "".join(literals).lower()
    for token, reason in _SENSITIVE_TOKENS:
        if re.search(rf"\b{re.escape(token)}\b", expression, re.I):
            return reason
        lowered = token.lower()
        if any(lowered in literal.lower() for literal in literals) or lowered in joined:
            return reason
    return None


def evaluate_policy_error(expression: str) -> Optional[str]:
    """Apply the optional sensitive-JavaScript vocabulary restriction."""
    try:
        from hermes_cli.config import read_raw_config

        config = read_raw_config()
        restricted = is_truthy_value(
            cfg_get(config, "browser", "restrict_evaluate"),
            default=False,
        )
        unsafe_override = is_truthy_value(
            cfg_get(config, "browser", "allow_unsafe_evaluate"),
            default=False,
        )
    except Exception:
        restricted = False
        unsafe_override = False
    if not restricted or unsafe_override:
        return None
    reason = risky_evaluate_reason(expression)
    if not reason:
        return None
    return (
        "Blocked: browser_console(expression=...) tried to use sensitive "
        f"JavaScript primitive ({reason}) while browser.restrict_evaluate is "
        "enabled. Use snapshot/image tools for normal inspection, or disable "
        "browser.restrict_evaluate only for a trusted page."
    )
