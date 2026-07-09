"""Gateway help and command-listing command ownership."""

from __future__ import annotations

from agent.i18n import t
from channels.platforms.base import MessageEvent
from hermes_gateway.config import Platform
from hermes_gateway.output_policy import telegramize_command_mentions


class GatewayCommandListingMixin:
    async def _handle_help_command(self, event: MessageEvent) -> str:
        """List available gateway commands."""
        from hermes_cli.commands import gateway_help_lines

        lines = [
            t("gateway.help.header"),
            *gateway_help_lines(),
        ]
        try:
            from agent.skill_commands import get_skill_commands
            skill_cmds = get_skill_commands()
            if skill_cmds:
                lines.append(t("gateway.help.skill_header", count=len(skill_cmds)))
                sorted_cmds = sorted(skill_cmds)
                for cmd in sorted_cmds[:10]:
                    lines.append(f"`{cmd}` — {skill_cmds[cmd]['description']}")
                if len(sorted_cmds) > 10:
                    lines.append(t("gateway.help.more_use_commands", count=len(sorted_cmds) - 10))
        except Exception:
            pass
        return telegramize_command_mentions(
            "\n".join(lines),
            getattr(getattr(event, "source", None), "platform", None),
        )

    async def _handle_commands_command(self, event: MessageEvent) -> str:
        from hermes_cli.commands import gateway_help_lines

        raw_args = event.get_command_args().strip()
        if raw_args:
            try:
                requested_page = int(raw_args)
            except ValueError:
                return t("gateway.commands.usage")
        else:
            requested_page = 1

        entries = list(gateway_help_lines())
        try:
            from agent.skill_commands import get_skill_commands
            skill_cmds = get_skill_commands()
            if skill_cmds:
                entries.append("")
                entries.append(t("gateway.commands.skill_header"))
                for cmd in sorted(skill_cmds):
                    desc = skill_cmds[cmd].get("description", "").strip() or t("gateway.commands.default_desc")
                    entries.append(f"`{cmd}` — {desc}")
        except Exception:
            pass

        if not entries:
            return t("gateway.commands.none")

        page_size = 15 if event.source.platform == Platform.TELEGRAM else 20
        total_pages = max(1, (len(entries) + page_size - 1) // page_size)
        page = max(1, min(requested_page, total_pages))
        start = (page - 1) * page_size
        page_entries = entries[start:start + page_size]

        lines = [
            t("gateway.commands.header", total=len(entries), page=page, total_pages=total_pages),
            "",
            *page_entries,
        ]
        if total_pages > 1:
            nav_parts = []
            if page > 1:
                nav_parts.append(t("gateway.commands.nav_prev", page=page - 1))
            if page < total_pages:
                nav_parts.append(t("gateway.commands.nav_next", page=page + 1))
            lines.extend(["", " | ".join(nav_parts)])
        if page != requested_page:
            lines.append(t("gateway.commands.out_of_range", requested=requested_page, page=page))
        return telegramize_command_mentions(
            "\n".join(lines),
            getattr(getattr(event, "source", None), "platform", None),
        )
