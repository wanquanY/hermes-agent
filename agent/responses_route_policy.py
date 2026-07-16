"""Route-specific replay policy for Responses-compatible providers.

The policy is resolved once at the provider boundary and then reused by
message conversion and the final request preflight.  Adapter code must not
infer replay safety from a collection of independent boolean flags.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping
from urllib.parse import urlparse


class ResponsesRouteKind(str, Enum):
    OPENAI_CODEX = "openai_codex"
    GITHUB_COPILOT = "github_copilot"
    XAI = "xai"
    OTHER = "other"


@dataclass(frozen=True)
class ResponsesRoutePolicy:
    kind: ResponsesRouteKind
    issuer_kind: str
    replay_encrypted_reasoning: bool = True
    replay_assistant_message_ids: bool = True
    max_assistant_message_id_chars: int = 64

    def replayable_message_item_id(self, value: Any) -> str | None:
        """Return a safe replay id, or ``None`` when this route forbids it."""
        if not self.replay_assistant_message_ids or not isinstance(value, str):
            return None
        candidate = value.strip()
        if not candidate or len(candidate) > self.max_assistant_message_id_chars:
            return None
        return candidate


def _normalized_host(base_url: Any) -> str:
    raw = str(base_url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    return str(parsed.hostname or "").lower().rstrip(".")


def resolve_responses_route_policy(
    params: Mapping[str, Any] | None = None,
    *,
    replay_encrypted_reasoning: bool | None = None,
) -> ResponsesRoutePolicy:
    """Resolve one immutable policy from already-known provider metadata."""
    values = params or {}
    host = _normalized_host(values.get("base_url"))
    provider = str(values.get("provider") or "").strip().lower()

    if bool(values.get("is_github_responses")) or provider == "copilot" or host == "api.githubcopilot.com":
        kind = ResponsesRouteKind.GITHUB_COPILOT
        issuer = "github_responses"
        replay_message_ids = False
    elif bool(values.get("is_xai_responses")) or provider in {"xai", "xai-oauth"} or host.endswith("x.ai"):
        kind = ResponsesRouteKind.XAI
        issuer = "xai_responses"
        replay_message_ids = True
    elif bool(values.get("is_codex_backend")) or provider == "openai-codex" or host == "chatgpt.com":
        kind = ResponsesRouteKind.OPENAI_CODEX
        issuer = "codex_backend"
        replay_message_ids = True
    else:
        kind = ResponsesRouteKind.OTHER
        issuer = f"other:{values.get('base_url')}" if values.get("base_url") else "other"
        replay_message_ids = True

    replay_reasoning = (
        bool(values.get("replay_encrypted_reasoning", True))
        if replay_encrypted_reasoning is None
        else bool(replay_encrypted_reasoning)
    )
    return ResponsesRoutePolicy(
        kind=kind,
        issuer_kind=issuer,
        replay_encrypted_reasoning=replay_reasoning,
        replay_assistant_message_ids=replay_message_ids,
    )


def coerce_responses_route_policy(
    policy: ResponsesRoutePolicy | None,
    *,
    is_xai_responses: bool = False,
    is_github_responses: bool = False,
    is_codex_backend: bool = False,
    base_url: str | None = None,
    replay_encrypted_reasoning: bool = True,
) -> ResponsesRoutePolicy:
    """Compatibility boundary for legacy adapter callers."""
    if isinstance(policy, ResponsesRoutePolicy):
        return policy
    return resolve_responses_route_policy(
        {
            "is_xai_responses": is_xai_responses,
            "is_github_responses": is_github_responses,
            "is_codex_backend": is_codex_backend,
            "base_url": base_url,
            "replay_encrypted_reasoning": replay_encrypted_reasoning,
        }
    )


__all__ = [
    "ResponsesRouteKind",
    "ResponsesRoutePolicy",
    "coerce_responses_route_policy",
    "resolve_responses_route_policy",
]
