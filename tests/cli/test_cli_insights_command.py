from unittest.mock import MagicMock, patch

from cli import HermesCLI


class _InsightsEngineStub:
    calls = []

    def __init__(self, analytics):
        self.analytics = analytics

    def generate(self, *, days=30, source=None):
        self.calls.append({"days": days, "source": source})
        return {"days": days, "source": source}

    def format_terminal(self, report):
        return f"days={report['days']} source={report['source']}"


def _run_show_insights(command: str):
    cli_obj = HermesCLI.__new__(HermesCLI)
    store = MagicMock()
    _InsightsEngineStub.calls = []
    with patch("cli.open_cli_session_store", return_value=store), \
         patch("agent.insights.InsightsEngine", _InsightsEngineStub):
        cli_obj._show_insights(command)
    return _InsightsEngineStub.calls, store


def test_cli_insights_accepts_positional_days(capsys):
    calls, store = _run_show_insights("/insights 7")

    assert calls == [{"days": 7, "source": None}]
    store.close.assert_called_once()
    assert "days=7 source=None" in capsys.readouterr().out


def test_cli_insights_keeps_days_flag_and_source(capsys):
    calls, store = _run_show_insights("/insights --days 14 --source discord")

    assert calls == [{"days": 14, "source": "discord"}]
    store.close.assert_called_once()
    assert "days=14 source=discord" in capsys.readouterr().out
