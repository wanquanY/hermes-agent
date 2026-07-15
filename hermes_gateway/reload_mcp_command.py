"""Gateway /reload-mcp command ownership."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from agent.i18n import t
from channels.platforms.base import MessageEvent

logger = logging.getLogger(__name__)


class GatewayReloadMcpCommandService:
    def __init__(self, runner):
        self._runner = runner

    async def handle_reload_mcp_command(self, event: MessageEvent) -> Optional[str]:
        """Reconnect MCP servers after a prompt-cache invalidation confirmation."""

        source = event.source
        session_key = self._runner._session_key_for_source(source)

        user_config = self._runner._read_user_config()
        approvals = user_config.get("approvals") if isinstance(user_config, dict) else None
        confirm_required = True
        if isinstance(approvals, dict):
            confirm_required = bool(approvals.get("mcp_reload_confirm", True))

        if not confirm_required:
            return await self.execute_mcp_reload(event)

        async def _on_confirm(choice: str) -> Optional[str]:
            if choice == "cancel":
                return t("gateway.reload_mcp.cancelled")
            if choice == "always":
                try:
                    from cli import save_config_value
                    save_config_value("approvals.mcp_reload_confirm", False)
                    logger.info(
                        "User opted out of /reload-mcp confirmation (session=%s)",
                        session_key,
                    )
                except Exception as exc:
                    logger.warning("Failed to persist mcp_reload_confirm=false: %s", exc)
            result = await self.execute_mcp_reload(event)
            if choice == "always":
                return f"{result}\n\n" + t("gateway.reload_mcp.always_followup")
            return result

        from channels.slash_commands import request_slash_confirm

        return await request_slash_confirm(
            runtime=self._runner._slash_confirmation_runtime(),
            event=event,
            command="reload-mcp",
            title="/reload-mcp",
            message=t("gateway.reload_mcp.confirm_prompt"),
            handler=_on_confirm,
        )

    async def execute_mcp_reload(self, event: MessageEvent) -> str:
        """Disconnect, reconnect, and notify the active session of MCP changes."""

        loop = asyncio.get_running_loop()
        try:
            from tools.mcp_tool import shutdown_mcp_servers, discover_mcp_tools, _servers, _lock

            with _lock:
                old_servers = set(_servers.keys())

            await loop.run_in_executor(None, shutdown_mcp_servers)
            new_tools = await loop.run_in_executor(None, discover_mcp_tools)

            with _lock:
                connected_servers = set(_servers.keys())

            added = connected_servers - old_servers
            removed = old_servers - connected_servers
            reconnected = connected_servers & old_servers

            lines = [t("gateway.reload_mcp.header")]
            if reconnected:
                lines.append(t("gateway.reload_mcp.reconnected", names=", ".join(sorted(reconnected))))
            if added:
                lines.append(t("gateway.reload_mcp.added", names=", ".join(sorted(added))))
            if removed:
                lines.append(t("gateway.reload_mcp.removed", names=", ".join(sorted(removed))))
            if not connected_servers:
                lines.append(t("gateway.reload_mcp.none_connected"))
            else:
                lines.append(t("gateway.reload_mcp.tools_available", tools=len(new_tools), servers=len(connected_servers)))

            try:
                from tools.mcp_tool import refresh_agent_mcp_tools
                cache = getattr(self._runner, "_agent_cache", None)
                cache_lock = getattr(self._runner, "_agent_cache_lock", None)
                if cache_lock is not None and cache:
                    with cache_lock:
                        for entry in list(cache.values()):
                            try:
                                agent = entry[0] if isinstance(entry, tuple) else entry
                            except Exception:
                                continue
                            if agent is None:
                                continue
                            refresh_agent_mcp_tools(agent, quiet_mode=True)
            except Exception as exc:
                logger.debug(
                    "Failed to update cached agent tools after MCP reload: %s",
                    exc,
                )

            change_parts = []
            if added:
                change_parts.append(f"Added servers: {', '.join(sorted(added))}")
            if removed:
                change_parts.append(f"Removed servers: {', '.join(sorted(removed))}")
            if reconnected:
                change_parts.append(f"Reconnected servers: {', '.join(sorted(reconnected))}")
            tool_summary = f"{len(new_tools)} MCP tool(s) now available" if new_tools else "No MCP tools available"
            change_detail = ". ".join(change_parts) + ". " if change_parts else ""
            reload_msg = {
                "role": "user",
                "content": f"[IMPORTANT: MCP servers have been reloaded. {change_detail}{tool_summary}. The tool list for this conversation has been updated accordingly.]",
            }
            try:
                session_entry = self._runner.session_store.get_or_create_session(event.source)
                self._runner.session_store.append_to_transcript(
                    session_entry.session_id, reload_msg
                )
            except Exception as exc:
                logger.debug("Could not append MCP reload notice to transcript: %s", exc)

            return "\n".join(lines)

        except Exception as e:
            logger.warning("MCP reload failed: %s", e)
            return t("gateway.reload_mcp.failed", error=e)


def reload_mcp_command_for(runner) -> GatewayReloadMcpCommandService:
    service = getattr(runner, "reload_mcp_command", None)
    if isinstance(service, GatewayReloadMcpCommandService):
        return service
    service = GatewayReloadMcpCommandService(runner)
    runner.reload_mcp_command = service
    return service
