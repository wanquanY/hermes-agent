"""Persistence contracts for live TUI session configuration."""

from __future__ import annotations

import json
from types import SimpleNamespace

from hermes_agent.composition.cli_session_store import open_cli_session_store
from tui_gateway import server


def test_session_runtime_config_updates_model_and_preserves_it_when_omitted(tmp_path):
    store = open_cli_session_store(tmp_path / "sessions.db")
    try:
        store.sessions.create("session-1", source="tui", model="initial-model")

        assert store.sessions.update_runtime_config(
            "session-1",
            {"provider": "custom", "base_url": "https://example.invalid"},
            model="next-model",
        )
        assert store.sessions.update_runtime_config(
            "session-1",
            {"provider": "custom", "service_tier": "priority"},
        )

        row = store.sessions.get("session-1")
        assert row is not None
        assert row["model"] == "next-model"
        assert json.loads(row["model_config"]) == {
            "provider": "custom",
            "service_tier": "priority",
        }
    finally:
        store.close()


def test_live_session_runtime_persists_through_session_component(tmp_path):
    store = open_cli_session_store(tmp_path / "sessions.db")
    try:
        store.sessions.create(
            "session-1",
            source="tui",
            model="initial-model",
            model_config={"retained": True, "base_url": "https://old.invalid"},
        )
        agent = SimpleNamespace(
            _session_db=store,
            model="next-model",
            provider="custom",
            base_url="",
            api_mode="responses",
            reasoning_config={"effort": "high"},
            service_tier="priority",
        )

        server._persist_live_session_runtime({"agent": agent, "session_key": "session-1"})

        row = store.sessions.get("session-1")
        assert row is not None
        assert row["model"] == "next-model"
        assert json.loads(row["model_config"]) == {
            "retained": True,
            "model": "next-model",
            "provider": "custom",
            "api_mode": "responses",
            "reasoning_config": {"effort": "high"},
            "service_tier": "priority",
        }
    finally:
        store.close()


def test_live_system_prompt_persists_through_session_component(tmp_path):
    store = open_cli_session_store(tmp_path / "sessions.db")
    try:
        store.sessions.create("session-1", source="tui")

        class _Agent:
            session_id = "session-1"

            def __init__(self):
                self._session_db = store
                self._cached_system_prompt = None

            def _build_system_prompt(self, _system_message):
                return "updated system prompt"

        agent = _Agent()
        server._persist_live_session_system_prompt(
            {"agent": agent, "session_key": "session-1"}
        )

        row = store.sessions.get("session-1")
        assert row is not None
        assert row["system_prompt"] == "updated system prompt"
        assert agent._cached_system_prompt == "updated system prompt"
    finally:
        store.close()
