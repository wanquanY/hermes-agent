import os

import pytest

from hermes_gateway.config import GatewayConfig, Platform, PlatformConfig
from hermes_gateway.goal_commands import goal_command_for
from channels.platforms.base import MessageEvent, MessageType
from hermes_gateway.runner import GatewayRunner
from hermes_gateway.session import SessionSource
from hermes_cli import goals


class _FakeSessionEntry:
    session_id = "sid-gateway-goal-config"


class _FakeSessionStore:
    def __init__(self):
        self.entry = _FakeSessionEntry()

    def get_or_create_session(self, source):
        return self.entry

    def _generate_session_key(self, source):
        return "agent:main:discord:channel:goal-config"


@pytest.mark.asyncio
async def test_gateway_goal_uses_goals_max_turns_from_full_config(tmp_path, monkeypatch):
    """Gateway /goal should honor top-level goals.max_turns from config.yaml."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("goals:\n  max_turns: 7\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._STORE_CACHE.clear()

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {}
    runner._queued_events = {}

    event = MessageEvent(
        text="/goal ship the benchmark",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="chat-goal-config",
            chat_type="channel",
            user_id="user-goal-config",
        ),
        message_id="msg-goal-config",
    )

    response = await goal_command_for(runner).handle_goal_command(event)

    try:
        assert "⊙ Goal set (7-turn budget): ship the benchmark" in response
        state = goals.GoalManager("sid-gateway-goal-config").state
        assert state is not None
        assert state.max_turns == 7
    finally:
        goals._STORE_CACHE.clear()


@pytest.mark.asyncio
async def test_gateway_goal_contract_and_wait_controls(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._STORE_CACHE.clear()

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {}
    runner._queued_events = {}
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="chat-goal-config",
        chat_type="channel",
        user_id="user-goal-config",
    )

    def event(text: str) -> MessageEvent:
        return MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id="msg-goal-config",
        )

    service = goal_command_for(runner)
    try:
        response = await service.handle_goal_command(
            event("/goal Ship release\nverify: pytest tests/release")
        )
        state = goals.GoalManager(_FakeSessionEntry.session_id).state
        assert state is not None
        assert state.goal == "Ship release"
        assert state.contract.verification == "pytest tests/release"
        assert "Completion contract" in response

        shown = await service.handle_goal_command(event("/goal show"))
        assert "Verification: pytest tests/release" in shown

        parked = await service.handle_goal_command(
            event(f"/goal wait {os.getpid()} release build")
        )
        assert "Goal parked" in parked
        waiting = goals.GoalManager(_FakeSessionEntry.session_id).state
        assert waiting is not None
        assert waiting.waiting_on_pid == os.getpid()

        released = await service.handle_goal_command(event("/goal unwait"))
        assert "Wait barrier cleared" in released
        resumed = goals.GoalManager(_FakeSessionEntry.session_id).state
        assert resumed is not None
        assert resumed.waiting_on_pid is None
    finally:
        goals._STORE_CACHE.clear()
