"""Tests for Unicode dash normalization in /insights command flag parsing.

Telegram on iOS auto-converts -- to em/en dashes. The /insights handler
normalizes these before parsing --days and --source flags.
"""
import pytest

from hermes_gateway.insights_command import normalize_insights_args


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
