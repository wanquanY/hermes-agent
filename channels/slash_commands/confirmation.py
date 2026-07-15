"""Slash-command confirmation primitive."""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Union

logger = logging.getLogger(__name__)

ConfirmHandler = Callable[[str], Awaitable[Any]]
ExecuteHandler = Callable[[], Awaitable[Any]]


@dataclass
class SlashConfirmationRuntime:
    """Dependencies needed to render and resolve a slash confirmation."""

    adapters: dict[Any, Any]
    session_key_for_source: Callable[[Any], str]
    read_user_config: Callable[[], dict[str, Any]]
    save_config_value: Callable[[str, Any], Any]
    thread_metadata_for_source: Callable[[Any, Any], Any]
    reply_anchor_for_event: Callable[[Any], Any]
    confirm_id_factory: Callable[[], str]


def counter_id_factory(counter: Any | None = None) -> Callable[[], str]:
    """Return a stable confirm-id factory backed by ``counter``."""
    if counter is None:
        counter = itertools.count(1)

    def _next() -> str:
        return str(next(counter))

    return _next


async def maybe_confirm_destructive_slash(
    *,
    runtime: SlashConfirmationRuntime,
    event: Any,
    command: str,
    title: str,
    detail: str,
    execute: ExecuteHandler,
) -> Union[str, Any, None]:
    """Gate a destructive session slash command before running ``execute``."""
    confirm_required = True
    try:
        cfg = runtime.read_user_config()
        approvals = cfg.get("approvals") if isinstance(cfg, dict) else None
        if isinstance(approvals, dict):
            confirm_required = bool(approvals.get("destructive_slash_confirm", True))
    except Exception:
        pass

    if not confirm_required:
        return await execute()

    session_key = runtime.session_key_for_source(event.source)

    async def _on_confirm(choice: str):
        if choice == "cancel":
            return f"🟡 /{command} cancelled. Conversation unchanged."
        if choice == "always":
            try:
                runtime.save_config_value("approvals.destructive_slash_confirm", False)
                logger.info(
                    "User opted out of destructive slash confirm (session=%s)",
                    session_key,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to persist destructive_slash_confirm=false: %s", exc,
                )
        result = await execute()
        if choice == "always":
            note = (
                "\n\nℹ️ Future /clear, /new, /reset, and /undo will run "
                "without confirmation. Re-enable via "
                "`approvals.destructive_slash_confirm: true` in config.yaml."
            )
            if isinstance(result, str):
                return result + note
            return result
        return result

    prompt_message = (
        f"⚠️ **Confirm /{command}**\n\n"
        f"{detail}\n\n"
        "Choose:\n"
        "• **Approve Once** — proceed this time only\n"
        "• **Always Approve** — proceed and silence this prompt permanently\n"
        "• **Cancel** — keep current conversation\n\n"
        "_Text fallback: reply `/approve`, `/always`, or `/cancel`._"
    )
    return await request_slash_confirm(
        runtime=runtime,
        event=event,
        command=command,
        title=title,
        message=prompt_message,
        handler=_on_confirm,
    )


async def request_slash_confirm(
    *,
    runtime: SlashConfirmationRuntime,
    event: Any,
    command: str,
    title: str,
    message: str,
    handler: ConfirmHandler,
) -> Optional[str]:
    """Ask the user to confirm a slash command side effect."""
    from tools import slash_confirm as slash_confirm_mod

    source = event.source
    session_key = runtime.session_key_for_source(source)
    confirm_id = runtime.confirm_id_factory()

    slash_confirm_mod.register(session_key, confirm_id, command, handler)

    adapter = runtime.adapters.get(source.platform)
    metadata = runtime.thread_metadata_for_source(
        source,
        runtime.reply_anchor_for_event(event),
    )

    used_buttons = False
    if adapter is not None:
        try:
            button_result = await adapter.send_slash_confirm(
                chat_id=source.chat_id,
                title=title,
                message=message,
                session_key=session_key,
                confirm_id=confirm_id,
                metadata=metadata,
            )
            if button_result and getattr(button_result, "success", False):
                used_buttons = True
        except Exception as exc:
            logger.debug(
                "send_slash_confirm failed for %s on %s: %s",
                command,
                source.platform,
                exc,
            )

    if used_buttons:
        return None
    return message


__all__ = [
    "SlashConfirmationRuntime",
    "counter_id_factory",
    "maybe_confirm_destructive_slash",
    "request_slash_confirm",
]
