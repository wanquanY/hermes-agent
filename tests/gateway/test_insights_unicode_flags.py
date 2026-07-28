"""Tests for Unicode dash normalization in /insights command flag parsing.

Telegram on iOS auto-converts -- to em/en dashes. The /insights handler
normalizes these before parsing --days and --source flags.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_gateway.insights_command import (
    GatewayInsightsCommandMixin,
    normalize_insights_args,
)


class TestInsightsUnicodeDashFlags:
    """--days and --source must survive iOS Unicode dash conversion."""

    @pytest.mark.parametrize("input_str,expected", [
        # Standard double hyphen (baseline)
        ("--days 7", "--days 7"),
        ("--source telegram", "--source telegram"),
        # Em dash (U+2014)
        ("\u2014days 7", "--days 7"),
        ("\u2014source telegram", "--source telegram"),
        # En dash (U+2013)
        ("\u2013days 7", "--days 7"),
        ("\u2013source telegram", "--source telegram"),
        # Figure dash (U+2012)
        ("\u2012days 7", "--days 7"),
        # Horizontal bar (U+2015)
        ("\u2015days 7", "--days 7"),
        # Combined flags with em dashes
        ("\u2014days 30 \u2014source cli", "--days 30 --source cli"),
    ])
    def test_unicode_dash_normalized(self, input_str, expected):
        result = normalize_insights_args(input_str)
        assert result == expected

    def test_regular_hyphens_unaffected(self):
        """Normal --days/--source must pass through unchanged."""
        assert normalize_insights_args("--days 7 --source discord") == "--days 7 --source discord"

    def test_bare_number_still_works(self):
        """Shorthand /insights 7 (no flag) must not be mangled."""
        assert normalize_insights_args("7") == "7"

    def test_no_flags_unchanged(self):
        """Input with no flags passes through as-is."""
        assert normalize_insights_args("") == ""
        assert normalize_insights_args("30") == "30"


@pytest.mark.asyncio
async def test_gateway_insights_uses_analytics_component_and_closes_store():
    analytics = object()
    store = SimpleNamespace(analytics=analytics, close=MagicMock())
    observed = {}

    class Engine:
        def __init__(self, component):
            observed["component"] = component

        def generate(self, *, days, source):
            observed["query"] = (days, source)
            return {"ok": True}

        def format_gateway(self, report):
            return "formatted" if report["ok"] else "invalid"

    event = SimpleNamespace(get_command_args=lambda: "--days 7 --source cli")
    handler = GatewayInsightsCommandMixin()
    with (
        patch(
            "hermes_gateway.insights_command.open_cli_session_store",
            return_value=store,
        ),
        patch("agent.insights.InsightsEngine", Engine),
    ):
        result = await handler._handle_insights_command(event)

    assert result == "formatted"
    assert observed == {"component": analytics, "query": (7, "cli")}
    store.close.assert_called_once_with()
