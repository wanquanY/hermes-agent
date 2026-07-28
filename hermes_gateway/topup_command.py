"""Gateway messaging ownership for the /topup command."""

from __future__ import annotations

import asyncio

from agent.i18n import t


def build_credits_view(*args, **kwargs):
    from agent.account_usage import build_credits_view as _build_credits_view

    return _build_credits_view(*args, **kwargs)


class GatewayTopupCommandService:
    """Render the browser-handoff billing view on every messaging platform."""

    def __init__(self, runner):
        self._runner = runner

    async def handle_topup_command(self, event) -> str:
        try:
            view = await asyncio.to_thread(build_credits_view, markdown=True)
        except Exception:
            view = None

        if view is None or not view.logged_in:
            return t("gateway.credits.not_logged_in")

        lines = ["💳 **Nous balance**"]
        lines.extend(
            line
            for line in view.balance_lines
            if not line.lstrip().startswith("📈")
        )
        if view.identity_line:
            lines.extend(("", view.identity_line))
        if view.topup_url:
            lines.extend(
                (
                    "",
                    f"Manage billing on the portal: {view.topup_url}",
                    "Top up and manage billing in the browser — "
                    "your balance updates here after.",
                )
            )
        return "\n".join(lines)


def topup_command_for(runner) -> GatewayTopupCommandService:
    service = getattr(runner, "topup_command", None)
    if service is None:
        service = GatewayTopupCommandService(runner)
        runner.topup_command = service
    return service
