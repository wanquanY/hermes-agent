"""Gateway session context prompt construction."""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional

from hermes_gateway.config import GatewayConfig, Platform
from channels.session_identity import (
    SessionContext,
    SessionSource,
    is_shared_multi_user_session,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PII redaction helpers
# ---------------------------------------------------------------------------

def _hash_id(value: str) -> str:
    """Deterministic 12-char hex hash of an identifier."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _hash_sender_id(value: str) -> str:
    """Hash a sender ID to ``user_<12hex>``."""
    return f"user_{_hash_id(value)}"


def _hash_chat_id(value: str) -> str:
    """Hash the numeric portion of a chat ID, preserving platform prefix.

    ``telegram:12345`` → ``telegram:<hash>``
    ``12345``          → ``<hash>``
    """
    colon = value.find(":")
    if colon > 0:
        prefix = value[:colon]
        return f"{prefix}:{_hash_id(value[colon + 1:])}"
    return _hash_id(value)


_PII_SAFE_PLATFORMS = frozenset({
    Platform.WHATSAPP,
    Platform.SIGNAL,
    Platform.TELEGRAM,
    Platform.BLUEBUBBLES,
})
"""Platforms where user IDs can be safely redacted (no in-message mention system
that requires raw IDs).  Discord is excluded because mentions use ``<@user_id>``
and the LLM needs the real ID to tag users."""


def _discord_tools_loaded() -> bool:
    """True iff the agent will actually have Discord tools this session.

    Two conditions must hold:
      1. The `discord` or `discord_admin` toolset is enabled for the
         Discord platform via `hermes tools` (opt-in, default OFF).
      2. `DISCORD_BOT_TOKEN` is set — the tool's `check_fn` gates on it
         at registry time, so the toolset being enabled in config is not
         enough if the token isn't configured.

    Returns False (safe default — keeps the stale-API disclaimer) on any
    error so a bad config can't silently promise tools the agent lacks.
    """
    if not (os.environ.get("DISCORD_BOT_TOKEN") or "").strip():
        return False
    try:
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools
        cfg = load_config()
        enabled = _get_platform_tools(cfg, "discord", include_default_mcp_servers=False)
        return "discord" in enabled or "discord_admin" in enabled
    except Exception:
        return False


def build_session_context_prompt(
    context: SessionContext,
    *,
    redact_pii: bool = False,
) -> str:
    """
    Build the dynamic system prompt section that tells the agent about its context.

    This is injected into the system prompt so the agent knows:
    - Where messages are coming from
    - What platforms are connected
    - Where it can deliver scheduled task outputs

    When *redact_pii* is True **and** the source platform is in
    ``_PII_SAFE_PLATFORMS``, phone numbers are stripped and user/chat IDs
    are replaced with deterministic hashes before being sent to the LLM.
    Platforms like Discord are excluded because mentions need real IDs.
    Routing still uses the original values (they stay in SessionSource).
    """
    # Only apply redaction on platforms where IDs aren't needed for mentions.
    # Check both the hardcoded set (builtins) and the plugin registry.
    _is_pii_safe = context.source.platform in _PII_SAFE_PLATFORMS
    if not _is_pii_safe:
        try:
            from channels.platform_registry import platform_registry
            entry = platform_registry.get(context.source.platform.value)
            if entry and entry.pii_safe:
                _is_pii_safe = True
        except Exception:
            logger.debug(
                "Failed to inspect plugin PII policy for platform %s",
                context.source.platform.value,
                exc_info=True,
            )
    redact_pii = redact_pii and _is_pii_safe
    lines = [
        "## Current Session Context",
        "",
    ]

    # Source info
    platform_name = context.source.platform.value.title()
    if context.source.platform == Platform.LOCAL:
        lines.append(f"**Source:** {platform_name} (the machine running this agent)")
    else:
        # Build a description that respects PII redaction
        src = context.source
        if redact_pii:
            # Build a safe description without raw IDs
            _uname = src.user_name or (
                _hash_sender_id(src.user_id) if src.user_id else "user"
            )
            _cname = src.chat_name or _hash_chat_id(src.chat_id)
            if src.chat_type == "dm":
                desc = f"DM with {_uname}"
            elif src.chat_type == "group":
                desc = f"group: {_cname}"
            elif src.chat_type == "channel":
                desc = f"channel: {_cname}"
            else:
                desc = _cname
        else:
            desc = src.description
        lines.append(f"**Source:** {platform_name} ({desc})")

    # Channel topic (if available - provides context about the channel's purpose)
    if context.source.chat_topic:
        lines.append(f"**Channel Topic:** {context.source.chat_topic}")

    # User identity.
    # In shared multi-user sessions (shared threads OR shared non-thread groups
    # when group_sessions_per_user=False), multiple users contribute to the same
    # conversation.  Don't pin a single user name in the system prompt — it
    # changes per-turn and would bust the prompt cache.  Instead, note that
    # this is a multi-user session; individual sender names are prefixed on
    # each user message by the gateway.
    if context.shared_multi_user_session:
        session_label = "Multi-user thread" if context.source.thread_id else "Multi-user session"
        lines.append(
            f"**Session type:** {session_label} — messages are prefixed "
            "with [sender name]. Multiple users may participate."
        )
    elif context.source.user_name:
        lines.append(f"**User:** {context.source.user_name}")
    elif context.source.user_id:
        uid = context.source.user_id
        if redact_pii:
            uid = _hash_sender_id(uid)
        lines.append(f"**User ID:** {uid}")

    # Platform-specific behavioral notes
    if context.source.platform == Platform.SLACK:
        lines.append("")
        lines.append(
            "**Platform notes:** You are running inside Slack. "
            "You do NOT have access to Slack-specific APIs — you cannot search "
            "channel history, pin/unpin messages, manage channels, or list users. "
            "Do not promise to perform these actions. The gateway may inline the "
            "current message's Slack block/attachment payload when available, but "
            "you still cannot call Slack APIs yourself."
        )
    elif context.source.platform == Platform.DISCORD:
        # Inject the Discord IDs block only when the agent actually has
        # Discord tools loaded this session — i.e. the user opted into
        # `discord` / `discord_admin` via `hermes tools` AND the bot
        # token is configured.  Otherwise keep the stale-API disclaimer
        # honest so we never promise tools the agent lacks.
        if _discord_tools_loaded():
            src = context.source
            id_lines = ["", "**Discord IDs (for the `discord` / `discord_admin` tools):**"]
            if src.guild_id:
                id_lines.append(f"  - Guild: `{src.guild_id}`")
            if src.thread_id and src.parent_chat_id:
                id_lines.append(f"  - Parent channel: `{src.parent_chat_id}`")
                id_lines.append(f"  - Thread: `{src.thread_id}` (use as `channel_id` for fetch_messages etc.)")
            else:
                id_lines.append(f"  - Channel: `{src.chat_id}`")
            if src.message_id:
                id_lines.append(
                    "  - Triggering message: provided per-turn in the incoming "
                    "user message (use it as `message_id` for reply/react/pin)"
                )
            lines.extend(id_lines)
        else:
            lines.append("")
            lines.append(
                "**Platform notes:** You are running inside Discord. "
                "You do NOT have access to Discord-specific APIs — you cannot search "
                "channel history, pin messages, manage roles, or list server members. "
                "Do not promise to perform these actions. If the user asks, explain "
                "that you can only read messages sent directly to you and respond."
            )
        lines.append("")
        lines.append(
            "Voice-channel state, when relevant, appears in the current "
            "message as a `[Voice channel now: ...]` note."
        )
    elif context.source.platform == Platform.BLUEBUBBLES:
        lines.append("")
        lines.append(
            "**Platform notes:** You are responding via iMessage. "
            "Keep responses short and conversational — think texts, not essays. "
            "Structure longer replies as separate short thoughts, each separated "
            "by a blank line (double newline). Each block between blank lines "
            "will be delivered as its own iMessage bubble, so write accordingly: "
            "one idea per bubble, 1–3 sentences each. "
            "If the user needs a detailed answer, give the short version first "
            "and offer to elaborate."
        )
    elif context.source.platform == Platform.YUANBAO:
        lines.append("")
        lines.append(
            "**Platform notes:** You are running inside Yuanbao. "
            "You CAN send private (DM) messages via the send_message tool. "
            "Use target='yuanbao:direct:<account_id>' for DM "
            "and target='yuanbao:group:<group_code>' for group chat."
        )

    # Connected platforms
    platforms_list = ["local (files on this machine)"]
    for p in context.connected_platforms:
        if p != Platform.LOCAL:
            platforms_list.append(f"{p.value}: Connected ✓")

    lines.append(f"**Connected Platforms:** {', '.join(platforms_list)}")

    # Home channels
    if context.home_channels:
        lines.append("")
        lines.append("**Home Channels (default destinations):**")
        for platform, home in context.home_channels.items():
            hc_id = _hash_chat_id(home.chat_id) if redact_pii else home.chat_id
            lines.append(f"  - {platform.value}: {home.name} (ID: {hc_id})")

    # Delivery options for scheduled tasks
    lines.append("")
    lines.append("**Delivery options for scheduled tasks:**")

    from hermes_constants import display_hermes_home

    # Origin delivery
    if context.source.platform == Platform.LOCAL:
        lines.append("- `\"origin\"` → Local output (saved to files)")
    else:
        _origin_label = context.source.chat_name or (
            _hash_chat_id(context.source.chat_id) if redact_pii else context.source.chat_id
        )
        lines.append(f"- `\"origin\"` → Back to this chat ({_origin_label})")

    # Local always available
    lines.append(
        f"- `\"local\"` → Save to local files only ({display_hermes_home()}/cron/output/)"
    )

    # Platform home channels
    for platform, home in context.home_channels.items():
        lines.append(f"- `\"{platform.value}\"` → Home channel ({home.name})")

    # Note about explicit targeting
    lines.append("")
    lines.append("*For explicit targeting, use `\"platform:chat_id\"` format if the user provides a specific chat ID.*")

    return "\n".join(lines)



def build_session_context(
    source: SessionSource,
    config: GatewayConfig,
    session_entry: Optional[SessionEntry] = None
) -> SessionContext:
    """
    Build a full session context from a source and config.
    
    This is used to inject context into the agent's system prompt.
    """
    connected = config.get_connected_platforms()
    
    home_channels = {}
    for platform in connected:
        home = config.get_home_channel(platform)
        if home:
            home_channels[platform] = home
    
    context = SessionContext(
        source=source,
        connected_platforms=connected,
        home_channels=home_channels,
        shared_multi_user_session=is_shared_multi_user_session(
            source,
            group_sessions_per_user=getattr(config, "group_sessions_per_user", True),
            thread_sessions_per_user=getattr(config, "thread_sessions_per_user", False),
        ),
    )
    
    if session_entry:
        context.session_key = session_entry.session_key
        context.session_id = session_entry.session_id
        context.created_at = session_entry.created_at
        context.updated_at = session_entry.updated_at
    
    return context
