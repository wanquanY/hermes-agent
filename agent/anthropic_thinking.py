"""Anthropic thinking-mode classification and replay normalization.

The transport adapter owns wire-shape conversion.  This module owns the
independent policy for choosing modern/adaptive thinking and for deciding
which persisted thinking blocks are safe to replay to a target endpoint.
"""

from __future__ import annotations

from typing import Any

from utils import base_url_host_matches


LEGACY_MANUAL_THINKING_CLAUDE_SUBSTRINGS = (
    "claude-3",
    "claude-opus-4-0",
    "claude-opus-4.0",
    "claude-opus-4-1",
    "claude-opus-4.1",
    "claude-sonnet-4-0",
    "claude-sonnet-4.0",
    "claude-opus-4-2025",
    "claude-sonnet-4-2025",
    "claude-opus-4-5",
    "claude-opus-4.5",
    "claude-sonnet-4-5",
    "claude-sonnet-4.5",
    "claude-haiku-4-5",
    "claude-haiku-4.5",
)

NO_XHIGH_CLAUDE_SUBSTRINGS = (
    "claude-opus-4-6",
    "claude-opus-4.6",
    "claude-sonnet-4-6",
    "claude-sonnet-4.6",
)

FAST_MODE_SUPPORTED_SUBSTRINGS = ("opus-4-6", "opus-4.6")

KIMI_FAMILY_MODEL_PREFIXES = (
    "kimi-",
    "kimi_",
    "moonshot-",
    "moonshot_",
    "k1.",
    "k1-",
    "k2.",
    "k2-",
    "k25",
    "k2.5",
)


def model_name_is_kimi_family(model: str | None) -> bool:
    if not isinstance(model, str):
        return False
    normalized = model.strip().lower()
    if not normalized:
        return False
    if "/" in normalized:
        normalized = normalized.rsplit("/", 1)[-1]
    return normalized.startswith(KIMI_FAMILY_MODEL_PREFIXES)


def is_kimi_family_endpoint(
    base_url: str | None,
    model: str | None = None,
) -> bool:
    normalized = str(base_url or "").strip().rstrip("/").lower()
    if normalized.startswith("https://api.kimi.com/coding"):
        return True
    if any(
        base_url_host_matches(base_url or "", domain)
        for domain in ("api.kimi.com", "moonshot.ai", "moonshot.cn")
    ):
        return True
    return model_name_is_kimi_family(model)


def is_deepseek_anthropic_endpoint(base_url: str | None) -> bool:
    if not base_url_host_matches(base_url or "", "api.deepseek.com"):
        return False
    normalized = str(base_url or "").strip().rstrip("/").lower()
    return "/anthropic" in normalized


def supports_adaptive_thinking(model: str) -> bool:
    """Use the modern contract for Kimi and all non-legacy Claude models."""
    if model_name_is_kimi_family(model):
        return True
    normalized = (model or "").lower()
    if "claude" not in normalized:
        return False
    return not any(
        marker in normalized
        for marker in LEGACY_MANUAL_THINKING_CLAUDE_SUBSTRINGS
    )


def supports_xhigh_effort(model: str) -> bool:
    if not supports_adaptive_thinking(model):
        return False
    normalized = (model or "").lower()
    return not any(marker in normalized for marker in NO_XHIGH_CLAUDE_SUBSTRINGS)


def forbids_sampling_params(model: str) -> bool:
    """Modern Claude models reject explicit sampling parameters."""
    normalized = (model or "").lower()
    if "claude" not in normalized:
        return False
    if any(marker in normalized for marker in NO_XHIGH_CLAUDE_SUBSTRINGS):
        return False
    return not any(
        marker in normalized
        for marker in LEGACY_MANUAL_THINKING_CLAUDE_SUBSTRINGS
    )


def supports_fast_mode(model: str) -> bool:
    return any(marker in model for marker in FAST_MODE_SUPPORTED_SUBSTRINGS)


def normalize_replayed_thinking(
    messages: list[dict[str, Any]],
    *,
    third_party: bool,
    kimi_family: bool,
    deepseek_anthropic: bool,
) -> None:
    """Normalize persisted thinking blocks for the destination endpoint.

    Kimi accepts its signed and unsigned thinking blocks as-is.  DeepSeek can
    replay unsigned blocks but cannot validate signatures.  Other third-party
    endpoints receive no Anthropic-proprietary blocks.  Native Anthropic keeps
    only a valid latest-turn signature.
    """
    thinking_types = frozenset(("thinking", "redacted_thinking"))
    last_assistant_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].get("role") == "assistant"
        ),
        None,
    )

    for index, message in enumerate(messages):
        content = message.get("content")
        if message.get("role") != "assistant" or not isinstance(content, list):
            continue

        if kimi_family:
            pass
        elif deepseek_anthropic:
            retained = []
            for block in content:
                if not isinstance(block, dict) or block.get("type") not in thinking_types:
                    retained.append(block)
                elif not block.get("signature") and not block.get("data"):
                    retained.append(block)
            message["content"] = retained or [{"type": "text", "text": "(empty)"}]
        elif third_party or index != last_assistant_index:
            retained = [
                block
                for block in content
                if not (
                    isinstance(block, dict)
                    and block.get("type") in thinking_types
                )
            ]
            message["content"] = retained or [
                {"type": "text", "text": "(thinking elided)"}
            ]
        else:
            signature_invalidated = bool(
                message.get("_thinking_signature_invalidated")
            )
            retained = []
            for block in content:
                if not isinstance(block, dict) or block.get("type") not in thinking_types:
                    retained.append(block)
                    continue
                if signature_invalidated:
                    thinking_text = block.get("thinking", "")
                    if thinking_text:
                        retained.append({"type": "text", "text": thinking_text})
                    continue
                if block.get("type") == "redacted_thinking":
                    if block.get("data"):
                        retained.append(block)
                elif block.get("signature"):
                    retained.append(block)
                else:
                    thinking_text = block.get("thinking", "")
                    if thinking_text:
                        retained.append({"type": "text", "text": thinking_text})
            message["content"] = retained or [{"type": "text", "text": "(empty)"}]

        for block in message["content"]:
            if isinstance(block, dict) and block.get("type") in thinking_types:
                block.pop("cache_control", None)
        message.pop("_thinking_signature_invalidated", None)


def evict_old_screenshots(
    messages: list[dict[str, Any]],
    *,
    keep: int = 3,
) -> None:
    """Replace older computer-use screenshots with a compact placeholder."""
    image_count = 0
    for message in reversed(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            inner = block.get("content")
            if not isinstance(inner, list) or not any(
                isinstance(item, dict) and item.get("type") == "image"
                for item in inner
            ):
                continue
            image_count += 1
            if image_count > keep:
                block["content"] = [
                    item
                    if item.get("type") != "image"
                    else {
                        "type": "text",
                        "text": "[screenshot removed to save context]",
                    }
                    for item in inner
                ]


__all__ = [
    "evict_old_screenshots",
    "forbids_sampling_params",
    "is_deepseek_anthropic_endpoint",
    "is_kimi_family_endpoint",
    "model_name_is_kimi_family",
    "normalize_replayed_thinking",
    "supports_adaptive_thinking",
    "supports_fast_mode",
    "supports_xhigh_effort",
]
