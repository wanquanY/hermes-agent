"""Gateway /reload-skills command ownership."""

from __future__ import annotations

import asyncio
import inspect
import logging

from agent.i18n import t
from channels.platforms.base import MessageEvent

logger = logging.getLogger(__name__)


def _format_skill_item(item: dict) -> str:
    name = item.get("name", "")
    desc = item.get("description", "")
    if desc:
        return t("gateway.reload_skills.item_with_desc", name=name, desc=desc)
    return t("gateway.reload_skills.item_no_desc", name=name)


class GatewayReloadSkillsCommandService:
    def __init__(self, runner):
        self._runner = runner

    async def handle_reload_skills_command(self, event: MessageEvent) -> str:
        """Rescan skills, refresh adapter state, and queue a next-turn note."""

        loop = asyncio.get_running_loop()
        try:
            from agent.skill_commands import reload_skills

            result = await loop.run_in_executor(None, reload_skills)
            added = result.get("added", [])
            removed = result.get("removed", [])
            total = result.get("total", 0)

            for adapter in list(self._runner.adapters.values()):
                refresh = getattr(adapter, "refresh_skill_group", None)
                if not callable(refresh):
                    continue
                try:
                    maybe = refresh()
                    if inspect.isawaitable(maybe):
                        await maybe
                except Exception as exc:
                    logger.warning(
                        "Adapter %s refresh_skill_group raised: %s",
                        getattr(adapter, "name", adapter),
                        exc,
                    )

            lines = [t("gateway.reload_skills.header")]
            if not added and not removed:
                lines.append(t("gateway.reload_skills.no_new"))
                lines.append(t("gateway.reload_skills.total", count=total))
                return "\n".join(lines)

            if added:
                lines.append(t("gateway.reload_skills.added_header"))
                for item in added:
                    lines.append(_format_skill_item(item))
            if removed:
                lines.append(t("gateway.reload_skills.removed_header"))
                for item in removed:
                    lines.append(_format_skill_item(item))
            lines.append(t("gateway.reload_skills.total", count=total))

            sections = ["[USER INITIATED SKILLS RELOAD:"]
            if added:
                sections.append("")
                sections.append("Added Skills:")
                for item in added:
                    sections.append(_format_skill_item(item))
            if removed:
                sections.append("")
                sections.append("Removed Skills:")
                for item in removed:
                    sections.append(_format_skill_item(item))
            sections.append("")
            sections.append("Use skills_list to see the updated catalog.]")
            note = "\n".join(sections)

            session_key = self._runner._session_key_for_source(event.source)
            if not hasattr(self._runner, "_pending_skills_reload_notes"):
                self._runner._pending_skills_reload_notes = {}
            if session_key:
                self._runner._pending_skills_reload_notes[session_key] = note

            return "\n".join(lines)

        except Exception as e:
            logger.warning("Skills reload failed: %s", e)
            return t("gateway.reload_skills.failed", error=e)


def reload_skills_command_for(runner) -> GatewayReloadSkillsCommandService:
    service = getattr(runner, "reload_skills_command", None)
    if isinstance(service, GatewayReloadSkillsCommandService):
        return service
    service = GatewayReloadSkillsCommandService(runner)
    runner.reload_skills_command = service
    return service
