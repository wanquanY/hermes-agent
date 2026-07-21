"""TUI fast-mode ownership is conversation-scoped unless global is explicit."""

from types import SimpleNamespace
from unittest.mock import patch

import tui_gateway.server as server


def _agent(service_tier=None):
    return SimpleNamespace(
        reasoning_config=None,
        service_tier=service_tier,
        request_overrides={},
        model="gpt-6",
        provider="openai",
        session_id="sess-key",
    )


def _set(params: dict) -> dict:
    return server._methods["config.set"]("rid-1", params)


def _get(params: dict) -> dict:
    return server._methods["config.get"]("rid-1", params)


class TestConfigSetFastSessionScope:
    def test_session_scoped_fast_skips_global_write(self) -> None:
        agent = _agent()
        session = {"session_key": "k1", "agent": agent}
        with (
            patch.dict(server._sessions, {"s1": session}, clear=False),
            patch.object(server, "_write_config_key") as write_key,
            patch.object(server, "_persist_live_session_runtime"),
            patch.object(server, "_emit"),
            patch(
                "hermes_cli.models.resolve_fast_mode_overrides",
                return_value={"service_tier": "priority"},
            ),
        ):
            response = _set(
                {"key": "fast", "session_id": "s1", "value": "fast"}
            )

        assert response["result"]["value"] == "fast"
        assert response["result"]["scope"] == "session"
        assert agent.service_tier == "priority"
        assert session["create_service_tier_override"] == "priority"
        write_key.assert_not_called()

    def test_session_scoped_normal_pins_explicit_normal(self) -> None:
        agent = _agent(service_tier="priority")
        session = {"session_key": "k2", "agent": agent}
        with (
            patch.dict(server._sessions, {"s2": session}, clear=False),
            patch.object(server, "_write_config_key") as write_key,
            patch.object(server, "_persist_live_session_runtime"),
            patch.object(server, "_emit"),
        ):
            response = _set(
                {"key": "fast", "session_id": "s2", "value": "normal"}
            )

        assert response["result"]["value"] == "normal"
        assert agent.service_tier is None
        assert session["create_service_tier_override"] == ""
        write_key.assert_not_called()

    def test_lazy_session_uses_its_model_and_keeps_pin(self) -> None:
        session = {
            "session_key": "k3",
            "agent": None,
            "model_override": {"model": "session-model", "provider": "openai"},
        }
        with (
            patch.dict(server._sessions, {"s3": session}, clear=False),
            patch.object(server, "_write_config_key") as write_key,
            patch(
                "hermes_cli.models.resolve_fast_mode_overrides",
                return_value={"service_tier": "priority"},
            ) as resolve,
        ):
            _set({"key": "fast", "session_id": "s3", "value": "fast"})

        resolve.assert_called_once_with("session-model")
        assert session["create_service_tier_override"] == "priority"
        write_key.assert_not_called()

    def test_toggle_reads_prebuild_pin(self) -> None:
        session = {
            "session_key": "k4",
            "agent": None,
            "create_service_tier_override": "priority",
        }
        with (
            patch.dict(server._sessions, {"s4": session}, clear=False),
            patch.object(server, "_write_config_key") as write_key,
        ):
            response = _set(
                {"key": "fast", "session_id": "s4", "value": "toggle"}
            )

        assert response["result"]["value"] == "normal"
        assert session["create_service_tier_override"] == ""
        write_key.assert_not_called()

    def test_explicit_global_persists_and_clears_session_pin(self) -> None:
        session = {
            "session_key": "k5",
            "agent": _agent(service_tier="priority"),
            "create_service_tier_override": "priority",
        }
        with (
            patch.dict(server._sessions, {"s5": session}, clear=False),
            patch.object(server, "_write_config_key") as write_key,
            patch.object(server, "_persist_live_session_runtime"),
            patch.object(server, "_emit"),
        ):
            response = _set(
                {
                    "key": "fast",
                    "session_id": "s5",
                    "value": "normal",
                    "scope": "global",
                }
            )

        assert response["result"]["scope"] == "global"
        assert "create_service_tier_override" not in session
        write_key.assert_called_once_with("agent.service_tier", "normal")


class TestConfigGetFastSessionScope:
    def test_reads_prebuild_pin(self) -> None:
        session = {
            "session_key": "k6",
            "agent": None,
            "create_service_tier_override": "priority",
        }
        with patch.dict(server._sessions, {"s6": session}, clear=False):
            response = _get({"key": "fast", "session_id": "s6"})
        assert response["result"]["value"] == "fast"

    def test_explicit_normal_pin_wins_over_global_with_live_agent(self) -> None:
        session = {
            "session_key": "k7",
            "agent": _agent(),
            "create_service_tier_override": "",
        }
        with (
            patch.dict(server._sessions, {"s7": session}, clear=False),
            patch.object(server, "_load_service_tier", return_value="priority"),
        ):
            response = _get({"key": "fast", "session_id": "s7"})
        assert response["result"]["value"] == "normal"
