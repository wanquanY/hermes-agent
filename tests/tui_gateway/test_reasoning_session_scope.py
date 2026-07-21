"""TUI reasoning effort follows the conversation runtime owner."""

from types import SimpleNamespace
from unittest.mock import patch

import tui_gateway.server as server


def _agent(reasoning_config):
    return SimpleNamespace(
        reasoning_config=reasoning_config,
        service_tier=None,
        model="glm-5",
        provider="zai",
        session_id="sess-key",
    )


def _set(params: dict) -> dict:
    return server._methods["config.set"]("rid-1", params)


def _get(params: dict) -> dict:
    return server._methods["config.get"]("rid-1", params)


class TestConfigSetReasoningSessionScope:
    def test_session_scoped_set_skips_global_write(self) -> None:
        agent = _agent(None)
        session = {"session_key": "k1", "agent": agent}
        with (
            patch.dict(server._sessions, {"s1": session}, clear=False),
            patch.object(server, "_write_config_key") as write_key,
            patch.object(server, "_persist_live_session_runtime"),
            patch.object(server, "_emit"),
        ):
            response = _set(
                {"key": "reasoning", "session_id": "s1", "value": "none"}
            )

        assert response["result"]["scope"] == "session"
        assert agent.reasoning_config == {"enabled": False}
        assert session["create_reasoning_override"] == {"enabled": False}
        write_key.assert_not_called()

    def test_lazy_session_keeps_create_override(self) -> None:
        session = {"session_key": "k2", "agent": None}
        with (
            patch.dict(server._sessions, {"s2": session}, clear=False),
            patch.object(server, "_write_config_key") as write_key,
        ):
            response = _set(
                {"key": "reasoning", "session_id": "s2", "value": "high"}
            )

        assert response["result"]["value"] == "high"
        assert session["create_reasoning_override"] == {
            "enabled": True,
            "effort": "high",
        }
        write_key.assert_not_called()

    def test_explicit_global_persists_and_clears_session_pin(self) -> None:
        agent = _agent({"enabled": True, "effort": "high"})
        session = {
            "session_key": "k3",
            "agent": agent,
            "create_reasoning_override": {"enabled": True, "effort": "high"},
        }
        with (
            patch.dict(server._sessions, {"s3": session}, clear=False),
            patch.object(server, "_write_config_key") as write_key,
            patch.object(server, "_persist_live_session_runtime"),
            patch.object(server, "_emit"),
        ):
            response = _set(
                {
                    "key": "reasoning",
                    "session_id": "s3",
                    "value": "low",
                    "scope": "global",
                }
            )

        assert response["result"]["scope"] == "global"
        assert "create_reasoning_override" not in session
        write_key.assert_called_once_with("agent.reasoning_effort", "low")


class TestConfigGetReasoningSessionScope:
    def test_reads_session_pin(self) -> None:
        session = {
            "session_key": "k4",
            "agent": None,
            "create_reasoning_override": {"enabled": False},
        }
        with patch.dict(server._sessions, {"s4": session}, clear=False):
            response = _get({"key": "reasoning", "session_id": "s4"})
        assert response["result"]["value"] == "none"

    def test_yaml_false_is_disabled_not_default(self) -> None:
        with patch.object(
            server,
            "_load_cfg",
            return_value={"agent": {"reasoning_effort": False}},
        ):
            response = _get({"key": "reasoning"})
        assert response["result"]["value"] == "none"
