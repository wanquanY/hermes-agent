"""CLI-to-gateway session handoff runtime."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from channels.platforms.base import MessageEvent
from hermes_gateway.config import Platform
from hermes_gateway.session import SessionSource, build_session_key

logger = logging.getLogger(__name__)


class GatewaySessionHandoffRuntimeService:
    def __init__(self, runner):
        self._runner = runner

    async def handoff_watcher(self, interval: float = 2.0) -> None:
        """Background task that processes pending CLI to gateway session handoffs."""
        await asyncio.sleep(5)
        runner = self._runner
        while runner._running:
            try:
                if runner._session_db is None:
                    await asyncio.sleep(interval)
                    continue
                pending = runner._session_db.list_pending_handoffs()
                for row in pending:
                    session_id = row.get("id")
                    if not session_id:
                        continue
                    if not runner._session_db.claim_handoff(session_id):
                        continue
                    try:
                        await self.process_handoff(row)
                        runner._session_db.complete_handoff(session_id)
                    except Exception as exc:
                        logger.warning(
                            "Handoff for session %s failed: %s",
                            session_id, exc, exc_info=True,
                        )
                        runner._session_db.fail_handoff(session_id, str(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Handoff watcher tick error: %s", exc, exc_info=True)
            await asyncio.sleep(interval)

    async def process_handoff(self, row: Dict[str, Any]) -> None:
        """Execute one handoff row. Raises on failure so the watcher can mark it failed."""
        runner = self._runner
        cli_session_id = row["id"]
        platform_name = (row.get("handoff_platform") or "").strip().lower()
        if not platform_name:
            raise RuntimeError("handoff_platform is empty")

        try:
            platform = Platform(platform_name)
        except (ValueError, KeyError):
            raise RuntimeError(f"unknown platform '{platform_name}'")

        adapter = runner.adapters.get(platform)
        if not adapter:
            raise RuntimeError(f"platform '{platform_name}' is not active in this gateway")

        home = runner.config.get_home_channel(platform)
        if not home or not home.chat_id:
            raise RuntimeError(
                f"no home channel configured for {platform_name}; "
                f"run /sethome on the desired chat first"
            )

        cli_title = row.get("title") or cli_session_id[:8]
        thread_name = f"Hermes — {cli_title}"
        try:
            new_thread_id = await adapter.create_handoff_thread(str(home.chat_id), thread_name)
        except Exception as exc:
            logger.debug(
                "Handoff: create_handoff_thread raised on %s: %s",
                platform_name, exc, exc_info=True,
            )
            new_thread_id = None

        effective_thread_id = new_thread_id or (str(home.thread_id) if home.thread_id else None)
        dest_chat_type = "thread" if new_thread_id else "dm"
        dest_source = SessionSource(
            platform=platform,
            chat_id=str(home.chat_id),
            chat_name=home.name,
            chat_type=dest_chat_type,
            user_id="system:handoff",
            user_name="Handoff",
            thread_id=effective_thread_id,
        )

        platform_cfg = runner.config.platforms.get(platform)
        extra = platform_cfg.extra if platform_cfg else {}
        session_key = build_session_key(
            dest_source,
            group_sessions_per_user=extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=extra.get("thread_sessions_per_user", False),
        )

        runner.session_store.get_or_create_session(dest_source)
        switched = runner.session_store.switch_session(session_key, cli_session_id)
        if switched is None:
            raise RuntimeError(f"could not switch session key {session_key} → {cli_session_id}")

        runner._evict_cached_agent(session_key)
        runner._release_running_agent_state(session_key)

        synthetic_text = (
            f"[Session was just handed off from CLI (\"{cli_title}\") to this "
            f"channel. The full prior conversation history is loaded above. "
            f"Briefly confirm you're working here and summarize what we were "
            f"working on, so the user can continue from this device.]"
        )
        synthetic_event = MessageEvent(
            text=synthetic_text,
            source=dest_source,
            internal=True,
        )

        logger.info(
            "Handoff: dispatching synthetic turn for CLI session %s → %s "
            "(home=%s, thread=%s, session_key=%s)",
            cli_session_id,
            platform_name,
            home.chat_id,
            effective_thread_id,
            session_key,
        )

        response_text = await runner._handle_message(synthetic_event)
        if not response_text:
            return

        send_metadata: Dict[str, Any] = {}
        if effective_thread_id:
            send_metadata["thread_id"] = effective_thread_id
        try:
            result = await adapter.send(
                chat_id=str(home.chat_id),
                content=response_text,
                metadata=send_metadata or None,
            )
        except Exception as exc:
            raise RuntimeError(f"adapter.send failed: {exc}") from exc

        if not getattr(result, "success", True):
            err = getattr(result, "error", "send returned success=False")
            raise RuntimeError(f"adapter.send failed: {err}")


def session_handoff_runtime_for(runner) -> GatewaySessionHandoffRuntimeService:
    service = getattr(runner, "session_handoff_runtime", None)
    if isinstance(service, GatewaySessionHandoffRuntimeService):
        return service
    service = GatewaySessionHandoffRuntimeService(runner)
    runner.session_handoff_runtime = service
    return service
