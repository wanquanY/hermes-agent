"""Gateway /insights command ownership."""

from __future__ import annotations

import asyncio
import logging
import re

from agent.i18n import t
from channels.platforms.base import MessageEvent
from hermes_agent.composition.cli_session_store import open_cli_session_store

logger = logging.getLogger(__name__)
_UNICODE_DASH_RE = re.compile(r"[\u2012\u2013\u2014\u2015](days|source)")


def normalize_insights_args(raw: str) -> str:
    """Normalize iOS/Telegram Unicode dash variants before flag parsing."""

    return _UNICODE_DASH_RE.sub(r"--\1", raw)


class GatewayInsightsCommandMixin:
    async def _handle_insights_command(self, event: MessageEvent) -> str:
        """Show usage insights and analytics."""

        args = normalize_insights_args(event.get_command_args().strip())
        days = 30
        source = None

        if args:
            parts = args.split()
            i = 0
            while i < len(parts):
                if parts[i] == "--days" and i + 1 < len(parts):
                    try:
                        days = int(parts[i + 1])
                    except ValueError:
                        return t("gateway.insights.invalid_days", value=parts[i + 1])
                    i += 2
                elif parts[i] == "--source" and i + 1 < len(parts):
                    source = parts[i + 1]
                    i += 2
                elif parts[i].isdigit():
                    days = int(parts[i])
                    i += 1
                else:
                    i += 1

        try:
            from agent.insights import InsightsEngine

            loop = asyncio.get_running_loop()

            def _run_insights():
                db = open_cli_session_store()
                try:
                    engine = InsightsEngine(db.analytics)
                    report = engine.generate(days=days, source=source)
                    return engine.format_gateway(report)
                finally:
                    db.close()

            return await loop.run_in_executor(None, _run_insights)
        except Exception as e:
            logger.error("Insights command error: %s", e, exc_info=True)
            return t("gateway.insights.error", error=e)
