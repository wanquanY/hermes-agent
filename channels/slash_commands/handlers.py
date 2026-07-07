"""Built-in slash-command handlers owned outside the gateway runner."""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from typing import Any, Optional

from agent.i18n import t

from .access import policy_for_source

logger = logging.getLogger(__name__)


def check_slash_access(
    *,
    gateway_config: Any,
    source: Any,
    canonical_cmd: str,
) -> Optional[str]:
    """Return a denial message when ``source`` cannot run ``canonical_cmd``."""
    if not canonical_cmd:
        return None
    policy = policy_for_source(gateway_config, source)
    if not policy.enabled or policy.can_run(getattr(source, "user_id", None), canonical_cmd):
        return None

    platform = getattr(source, "platform", None)
    platform_value = platform.value if hasattr(platform, "value") else str(platform or "?")
    logger.info(
        "Slash command /%s denied for %s:%s (not admin, not in user_allowed_commands)",
        canonical_cmd,
        platform_value,
        getattr(source, "user_id", None),
    )
    allowed_preview = sorted(policy.user_allowed_commands)
    if allowed_preview:
        suffix = (
            "You can run: "
            + ", ".join(f"/{command}" for command in allowed_preview[:12])
            + ("..." if len(allowed_preview) > 12 else "")
            + ". Use /whoami for the full list."
        )
    else:
        suffix = (
            "No slash commands are enabled for non-admins on this "
            "platform. Ask an admin to add you to allow_admin_from "
            "or to set user_allowed_commands."
        )
    return f"⛔ /{canonical_cmd} is admin-only here. {suffix}"


async def handle_whoami_command(*, gateway_config: Any, event: Any) -> str:
    """Handle /whoami and report slash-command access for the current scope."""
    source = event.source
    policy = policy_for_source(gateway_config, source)
    platform = source.platform.value if source and source.platform else "?"
    chat_type = (source.chat_type if source else "") or "dm"
    scope = "DM" if chat_type.lower() in {"dm", "direct", "private", ""} else "group/channel"
    user_id = (source.user_id if source else None) or "?"

    if not policy.enabled:
        return (
            f"**You** — {platform} ({scope})\n"
            f"User ID: `{user_id}`\n"
            f"Tier: unrestricted (no admin list configured for this scope)\n"
            f"Slash commands: all available"
        )

    if policy.is_admin(user_id):
        return (
            f"**You** — {platform} ({scope})\n"
            f"User ID: `{user_id}`\n"
            f"Tier: **admin**\n"
            f"Slash commands: all available"
        )

    floor = ["help", "whoami"]
    configured = sorted(policy.user_allowed_commands)
    seen: set[str] = set()
    runnable: list[str] = []
    for command in floor + configured:
        if command not in seen:
            seen.add(command)
            runnable.append(command)
    runnable_str = ", ".join(f"/{command}" for command in runnable) if runnable else "(none)"
    return (
        f"**You** — {platform} ({scope})\n"
        f"User ID: `{user_id}`\n"
        f"Tier: user\n"
        f"Slash commands you can run: {runnable_str}"
    )


async def handle_kanban_command(
    *,
    event: Any,
    notifier_profile: str | None = None,
    active_profile_name: Any | None = None,
) -> str:
    """Handle /kanban by delegating to the shared kanban CLI."""
    from hermes_cli.kanban import run_slash

    text = (event.text or "").strip()
    if text.startswith("/"):
        text = text.lstrip("/")
    if text.startswith("kanban"):
        text = text[len("kanban"):].lstrip()

    tokens = shlex.split(text) if text else []
    requested_board = None
    action = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--board":
            if index + 1 >= len(tokens):
                break
            requested_board = tokens[index + 1]
            index += 2
            continue
        if token.startswith("--board="):
            requested_board = token.split("=", 1)[1]
            index += 1
            continue
        action = token
        break

    is_create = action == "create"

    try:
        output = await asyncio.to_thread(run_slash, text)
    except Exception as exc:  # pragma: no cover - defensive
        return t("gateway.kanban.error_prefix", error=exc)

    if is_create and output:
        subscribed = await _auto_subscribe_created_task(
            event=event,
            output=output,
            requested_board=requested_board,
            notifier_profile=notifier_profile,
            active_profile_name=active_profile_name,
        )
        if subscribed:
            output = (
                output.rstrip()
                + "\n"
                + t("gateway.kanban.subscribed_suffix", task_id=subscribed)
            )

    if len(output) > 3800:
        output = output[:3800] + "\n" + t("gateway.kanban.truncated_suffix")
    return output or t("gateway.kanban.no_output")


async def _auto_subscribe_created_task(
    *,
    event: Any,
    output: str,
    requested_board: str | None,
    notifier_profile: str | None,
    active_profile_name: Any | None,
) -> str | None:
    match = re.search(r"Created\s+(t_[0-9a-f]+)\b", output)
    if not match:
        return None

    task_id = match.group(1)
    try:
        source = event.source
        platform = getattr(source, "platform", None)
        platform_str = (
            platform.value if hasattr(platform, "value") else str(platform or "")
        ).lower()
        chat_id = str(getattr(source, "chat_id", "") or "")
        thread_id = str(getattr(source, "thread_id", "") or "")
        user_id = str(getattr(source, "user_id", "") or "") or None
        if not platform_str or not chat_id:
            return None

        def _subscribe() -> None:
            from hermes_cli import kanban_db as kanban_db

            conn = kanban_db.connect(board=requested_board)
            try:
                profile = notifier_profile
                if profile is None and active_profile_name is not None:
                    profile = active_profile_name()
                kanban_db.add_notify_sub(
                    conn,
                    task_id=task_id,
                    platform=platform_str,
                    chat_id=chat_id,
                    thread_id=thread_id or None,
                    user_id=user_id,
                    notifier_profile=profile,
                )
            finally:
                conn.close()

        await asyncio.to_thread(_subscribe)
        return task_id
    except Exception as exc:  # pragma: no cover - notification is best effort
        logger.warning("kanban create auto-subscribe failed: %s", exc)
        return None
