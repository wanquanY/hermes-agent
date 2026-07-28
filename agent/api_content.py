"""Byte-stable provider replay sidecars.

Hermes stores clean conversation content for users and durable semantics, but
the provider-bound content can additionally contain ephemeral memory, plugin,
or runtime context. ``api_content`` stores that exact string beside the clean
row so a later turn can replay the provider prefix byte-for-byte.
"""

from __future__ import annotations

from typing import Any, Mapping, MutableMapping, Optional

from agent.memory_manager import build_memory_context_block, sanitize_context


def compose_user_api_content(
    content: Any,
    *,
    memory_context: str = "",
    plugin_context: str = "",
) -> Optional[str]:
    """Return the exact current-user string sent to the provider."""
    if not isinstance(content, str):
        return None
    injections: list[str] = []
    if memory_context:
        fenced = build_memory_context_block(memory_context)
        if fenced:
            injections.append(fenced)
    if plugin_context:
        injections.append(plugin_context)
    if not injections:
        return content
    return content + "\n\n" + "\n\n".join(injections)


def substitute_api_content(message: MutableMapping[str, Any]) -> Optional[str]:
    """Pop a sidecar from an API copy and substitute its exact content."""
    sidecar = message.pop("api_content", None)
    if (
        isinstance(sidecar, str)
        and sidecar
        and message.get("role") in {"user", "assistant"}
    ):
        message["content"] = sidecar
    return sidecar if isinstance(sidecar, str) else None


def drop_stale_api_content(message: MutableMapping[str, Any]) -> None:
    """Discard exact-wire bytes after the associated clean content changes."""
    message.pop("api_content", None)


def extract_api_content_sidecar(message: Mapping[str, Any]) -> Optional[str]:
    """Return a valid string sidecar for storage or cross-session copying."""
    sidecar = message.get("api_content")
    return sidecar if isinstance(sidecar, str) and sidecar else None


def api_content_for_storage(
    *,
    role: str,
    content: Any,
    explicit_sidecar: Any,
) -> Optional[str]:
    """Resolve the non-redundant exact-wire string to persist.

    Read models sanitize user/assistant content for safe display. If that
    normalization would change a provider-bound string, retain the original as
    the sidecar even when no ephemeral injection was present.
    """
    sidecar = explicit_sidecar if isinstance(explicit_sidecar, str) else None
    if sidecar == content:
        sidecar = None
    if (
        sidecar is None
        and role in {"user", "assistant"}
        and isinstance(content, str)
        and content
        and sanitize_context(content).strip() != content
    ):
        sidecar = content
    return sidecar


__all__ = [
    "api_content_for_storage",
    "compose_user_api_content",
    "drop_stale_api_content",
    "extract_api_content_sidecar",
    "substitute_api_content",
]
