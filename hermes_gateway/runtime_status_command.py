"""Gateway runtime status commands: /status, /agents, /stop."""

from __future__ import annotations

import logging
import time
from typing import Union

from agent.i18n import t
from channels.platforms.base import EphemeralReply, MessageEvent
from hermes_gateway.agent_cache import AGENT_PENDING_SENTINEL

logger = logging.getLogger(__name__)
_INTERRUPT_REASON_STOP = "Stop requested"


class GatewayRuntimeStatusCommandMixin:
    async def _handle_status_command(self, event: MessageEvent) -> str:
        """Show gateway/session status."""

        source = event.source
        session_entry = self.session_store.get_or_create_session(source)
        connected_platforms = [p.value for p in self.adapters.keys()]
        session_key = session_entry.session_key
        is_running = session_key in self._running_agents
        adapter = self.adapters.get(source.platform) if source else None
        queue_depth = self._queue_depth(session_key, adapter=adapter)

        title = None
        db_total_tokens = 0
        if self._session_db:
            try:
                title = self._session_db.get_session_title(session_entry.session_id)
            except Exception:
                title = None
            try:
                row = self._session_db.get_session(session_entry.session_id)
                if row:
                    db_total_tokens = (
                        (row.get("input_tokens") or 0)
                        + (row.get("output_tokens") or 0)
                        + (row.get("cache_read_tokens") or 0)
                        + (row.get("cache_write_tokens") or 0)
                        + (row.get("reasoning_tokens") or 0)
                    )
            except Exception:
                db_total_tokens = 0

        lines = [
            t("gateway.status.header"),
            "",
            t("gateway.status.session_id", session_id=session_entry.session_id),
        ]
        if title:
            lines.append(t("gateway.status.title", title=title))
        lines.extend([
            t("gateway.status.created", timestamp=session_entry.created_at.strftime("%Y-%m-%d %H:%M")),
            t("gateway.status.last_activity", timestamp=session_entry.updated_at.strftime("%Y-%m-%d %H:%M")),
            t("gateway.status.tokens", tokens=f"{db_total_tokens:,}"),
            t("gateway.status.agent_running", state=t("gateway.status.state_yes") if is_running else t("gateway.status.state_no")),
        ])
        if queue_depth:
            lines.append(t("gateway.status.queued", count=queue_depth))
        lines.extend([
            "",
            t("gateway.status.platforms", platforms=", ".join(connected_platforms)),
        ])

        try:
            from hermes_cli.session_recap import build_recap
            history = self.session_store.load_transcript(session_entry.session_id)
            recap = build_recap(
                history,
                session_title=title,
                session_id=session_entry.session_id,
                platform=source.platform.value if source else None,
            )
            if recap:
                lines.extend(["", recap])
        except Exception as exc:  # pragma: no cover
            logger.debug("build_recap failed in /status: %s", exc)

        return "\n".join(lines)

    async def _handle_agents_command(self, event: MessageEvent) -> str:
        """List active gateway agents and running tool processes."""
        from tools.process_registry import format_uptime_short, process_registry

        now = time.time()
        current_session_key = self._session_key_for_source(event.source)

        running_agents: dict = getattr(self, "_running_agents", {}) or {}
        running_started: dict = getattr(self, "_running_agents_ts", {}) or {}

        agent_rows: list[dict] = []
        for session_key, agent in running_agents.items():
            started = float(running_started.get(session_key, now))
            elapsed = max(0, int(now - started))
            is_pending = agent is AGENT_PENDING_SENTINEL
            agent_rows.append(
                {
                    "session_key": session_key,
                    "elapsed": elapsed,
                    "state": t("gateway.agents.state_starting") if is_pending else t("gateway.agents.state_running"),
                    "session_id": "" if is_pending else str(getattr(agent, "session_id", "") or ""),
                    "model": "" if is_pending else str(getattr(agent, "model", "") or ""),
                }
            )

        agent_rows.sort(key=lambda row: row["elapsed"], reverse=True)

        try:
            running_processes = [
                p for p in process_registry.list_sessions()
                if p.get("status") == "running"
            ]
        except Exception:
            running_processes = []

        background_tasks = [
            task for task in (getattr(self, "_background_tasks", set()) or set())
            if hasattr(task, "done") and not task.done()
        ]

        lines = [
            t("gateway.agents.header"),
            "",
            t("gateway.agents.active_agents", count=len(agent_rows)),
        ]

        if agent_rows:
            for idx, row in enumerate(agent_rows[:12], 1):
                current = t("gateway.agents.this_chat") if row["session_key"] == current_session_key else ""
                sid = f" · `{row['session_id']}`" if row["session_id"] else ""
                model = f" · `{row['model']}`" if row["model"] else ""
                lines.append(
                    f"{idx}. `{row['session_key']}` · {row['state']} · "
                    f"{format_uptime_short(row['elapsed'])}{sid}{model}{current}"
                )
            if len(agent_rows) > 12:
                lines.append(t("gateway.agents.more", count=len(agent_rows) - 12))

        lines.extend(["", t("gateway.agents.running_processes", count=len(running_processes))])
        if running_processes:
            for proc in running_processes[:12]:
                cmd = " ".join(str(proc.get("command", "")).split())
                if len(cmd) > 90:
                    cmd = cmd[:87] + "..."
                lines.append(
                    f"- `{proc.get('session_id', '?')}` · "
                    f"{format_uptime_short(int(proc.get('uptime_seconds', 0)))} · `{cmd}`"
                )
            if len(running_processes) > 12:
                lines.append(t("gateway.agents.more", count=len(running_processes) - 12))

        lines.extend(["", t("gateway.agents.async_jobs", count=len(background_tasks))])

        if not agent_rows and not running_processes and not background_tasks:
            lines.append("")
            lines.append(t("gateway.agents.none"))

        return "\n".join(lines)

    async def _handle_stop_command(self, event: MessageEvent) -> Union[str, EphemeralReply]:
        """Interrupt and unlock the current session if an agent is active."""

        source = event.source
        session_entry = self.session_store.get_or_create_session(source)
        session_key = session_entry.session_key

        agent = self._running_agents.get(session_key)
        if agent is AGENT_PENDING_SENTINEL:
            await self._interrupt_and_clear_session(
                session_key,
                source,
                interrupt_reason=_INTERRUPT_REASON_STOP,
                invalidation_reason="stop_command_pending",
            )
            logger.info("STOP (pending) for session %s — sentinel cleared", session_key)
            return EphemeralReply(t("gateway.stop.stopped_pending"))
        if agent:
            await self._interrupt_and_clear_session(
                session_key,
                source,
                interrupt_reason=_INTERRUPT_REASON_STOP,
                invalidation_reason="stop_command_handler",
            )
            return EphemeralReply(t("gateway.stop.stopped"))
        return t("gateway.stop.no_active")
