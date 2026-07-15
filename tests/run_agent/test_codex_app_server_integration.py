"""Integration test for the codex_app_server runtime path through AIAgent.

Verifies that:
  - api_mode='codex_app_server' is accepted on AIAgent construction
  - run_conversation() takes the early-return path and never enters the
    chat completions loop
  - Projected messages from a fake Codex session land in the messages list
  - tool_iterations from the codex session tick the skill nudge counter
  - Memory nudge counter ticks once per turn
  - The returned dict has the same shape as the chat_completions path
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import run_agent
from agent.transports.codex_app_server_session import CodexAppServerSession, TurnResult


class _UsageDB:
    def __init__(self):
        self.calls = []
        self.sessions = self

    def update_token_counts(self, session_id, **kwargs):
        self.calls.append({"session_id": session_id, **kwargs})


def _usage_agent(*, account_mode: str, model: str = "glm-5.2-polluted"):
    return SimpleNamespace(
        api_mode="codex_app_server",
        model=model,
        provider="openai-codex",
        base_url="",
        api_key="",
        codex_account_mode=account_mode,
        session_id="session-usage",
        _session_db=_UsageDB(),
        _session_db_created=True,
        _ensure_db_session=lambda: None,
        context_compressor=None,
        session_api_calls=0,
        session_prompt_tokens=0,
        session_completion_tokens=0,
        session_total_tokens=0,
        session_input_tokens=0,
        session_output_tokens=0,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_reasoning_tokens=0,
        session_estimated_cost_usd=0.0,
        session_cost_status="unknown",
        session_cost_source="",
    )


def _usage_turn(**kwargs):
    return TurnResult(
        token_usage_last={
            "totalTokens": 130,
            "inputTokens": 80,
            "cachedInputTokens": 20,
            "outputTokens": 25,
            "reasoningOutputTokens": 5,
        },
        **kwargs,
    )


@pytest.fixture
def fake_session(monkeypatch):
    """Replace CodexAppServerSession with a stub that returns a fixed
    TurnResult, so we can drive AIAgent without spawning real codex."""

    def fake_run_turn(self, user_input: str, **kwargs):
        return TurnResult(
            final_text=f"echo: {user_input}",
            projected_messages=[
                {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "exec_1", "type": "function",
                                 "function": {"name": "exec_command",
                                              "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "exec_1", "content": "ok"},
                {"role": "assistant", "content": f"echo: {user_input}"},
            ],
            tool_iterations=1,
            interrupted=False,
            error=None,
            turn_id="turn-stub-1",
            thread_id="thread-stub-1",
        )

    monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
    monkeypatch.setattr(
        CodexAppServerSession, "ensure_started", lambda self: "thread-stub-1"
    )


def _make_codex_agent():
    """Construct an AIAgent in codex_app_server mode without contacting any
    real provider. We pass api_mode explicitly so the constructor takes the
    fast path for direct credentials."""
    return run_agent.AIAgent(
        api_key="stub",
        base_url="https://stub.invalid",
        provider="openai",
        api_mode="codex_app_server",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )


def _make_team_codex_agent(tmp_path, *, session_id="team-session-team-conversation-cx-h3"):
    from hermes_agent.storage.cli_session_store import open_cli_session_store

    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create(session_id=session_id, source="team_mission", model="test")
    db.session_index.upsert(
        session_id=session_id,
        source="team_mission",
        session_kind="team_mission",
        conversation_kind="team",
        status="idle",
    )
    db.participants.upsert_conversation_participant(
        conversation_session_id=session_id,
        participant_id="user",
        role="user",
        display_name="",
    )
    db.participants.upsert_conversation_participant(
        conversation_session_id=session_id,
        participant_id="leader:team-1",
        role="leader",
        display_name="小多",
    )
    db.participants.upsert_conversation_participant(
        conversation_session_id=session_id,
        participant_id="member:codex-member",
        role="member",
        member_id="codex-member",
        display_name="Codex 成员",
    )
    db.participants.upsert_conversation_participant(
        conversation_session_id=session_id,
        participant_id="member:designer",
        role="member",
        member_id="designer",
        display_name="UI/UX设计师",
    )

    with patch("hermes_logging.setup_logging"):
        agent = _make_codex_agent()
    agent.session_id = session_id
    agent._session_db = db
    agent._session_db_created = True
    agent.codex_home = str(tmp_path / "codex-home")
    return agent, db


def _set_member_chat_dovie_context(monkeypatch, *, session_id, member_id="codex-member"):
    monkeypatch.setenv(
        "HERMES_DOVIE_PRODUCT_CONTEXT",
        json.dumps(
            {
                "team_mission": {
                    "kind": "member_chat",
                    "surface": "member_chat",
                    "conversation_session_id": session_id,
                    "member_id": member_id,
                }
            },
            ensure_ascii=False,
        ),
    )


def _append_team_context_message(
    db,
    session_id,
    *,
    content,
    participant_id,
    role="assistant",
    conversation_message_id=None,
    metadata=None,
):
    base_metadata = {
        "activity_kind": "leader_chat",
        "transcript_activity_kind": "leader_chat",
        "team_mission": {"kind": "leader_chat"},
    }
    if participant_id.startswith("member:"):
        base_metadata["activity_kind"] = "member_direct_chat"
        base_metadata["transcript_activity_kind"] = "member_direct_chat"
        base_metadata["team_mission"] = {"kind": "member_chat_response"}
    if role == "user":
        base_metadata["activity_kind"] = "member_direct_chat"
        base_metadata["transcript_activity_kind"] = "member_direct_chat"
        base_metadata["team_mission"] = {
            "kind": "member_chat_user",
            "target_member_id": "codex-member",
        }
    if metadata:
        base_metadata.update(metadata)
    return db.messages.append(
        session_id=session_id,
        role=role,
        content=content,
        participant_id=participant_id,
        conversation_message_id=conversation_message_id or f"cx-h3-{content}",
        metadata=base_metadata,
    )


def _install_capturing_codex(monkeypatch, turns=None, ensure_started=None):
    captured = []
    queued_turns = list(turns or [])

    def fake_run_turn(self, user_input: str, **kwargs):
        captured.append(user_input)
        if queued_turns:
            return queued_turns.pop(0)
        return TurnResult(
            final_text="done",
            projected_messages=[{"role": "assistant", "content": "done"}],
            turn_id="turn-ok",
            thread_id="thread-ok",
        )

    monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
    monkeypatch.setattr(
        CodexAppServerSession,
        "ensure_started",
        ensure_started or (lambda self: "thread-ok"),
    )
    return captured


def _run_codex_turn_no_persist(agent, text):
    with patch.object(agent, "_spawn_background_review", return_value=None), patch.object(
        agent, "_persist_session", return_value=None
    ):
        return agent.run_conversation(text)


def _context_watermark_file(agent):
    from pathlib import Path

    return Path(agent.codex_home) / "hermes_context_watermarks.json"


class TestApiModeAccepted:
    def test_api_mode_is_codex_app_server(self):
        with patch("hermes_logging.setup_logging"):
            agent = _make_codex_agent()
        assert agent.api_mode == "codex_app_server"


class TestRunConversationCodexPath:
    def test_run_conversation_returns_codex_shape(self, fake_session):
        agent = _make_codex_agent()
        # No background review fork during tests
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hello there")
        assert result["final_response"] == "echo: hello there"
        assert result["completed"] is True
        assert result["partial"] is False
        assert result["error"] is None
        assert result["api_calls"] == 1
        assert result["codex_thread_id"] == "thread-stub-1"
        assert result["codex_turn_id"] == "turn-stub-1"

    def test_codex_app_server_token_usage_updates_session_accounting(self, monkeypatch):
        def fake_run_turn(self, user_input: str, **kwargs):
            return TurnResult(
                final_text="done",
                projected_messages=[{"role": "assistant", "content": "done"}],
                turn_id="turn-usage-1",
                thread_id="thread-usage-1",
                token_usage_last={
                    "totalTokens": 130,
                    "inputTokens": 80,
                    "cachedInputTokens": 20,
                    "outputTokens": 25,
                    "reasoningOutputTokens": 5,
                },
                model_context_window=200000,
            )

        monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
        monkeypatch.setattr(
            CodexAppServerSession, "ensure_started", lambda self: "thread-usage-1"
        )
        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hello")

        assert result["api_calls"] == 1
        assert result["prompt_tokens"] == 100
        assert result["completion_tokens"] == 25
        assert result["total_tokens"] == 130
        assert result["input_tokens"] == 80
        assert result["output_tokens"] == 25
        assert result["cache_read_tokens"] == 20
        assert result["cache_write_tokens"] == 0
        assert result["reasoning_tokens"] == 5
        assert result["last_prompt_tokens"] == 100

        assert agent.session_api_calls == 1
        assert agent.session_prompt_tokens == 100
        assert agent.session_completion_tokens == 25
        assert agent.session_total_tokens == 130
        assert agent.session_input_tokens == 80
        assert agent.session_output_tokens == 25
        assert agent.session_cache_read_tokens == 20
        assert agent.session_cache_write_tokens == 0
        assert agent.session_reasoning_tokens == 5
        assert agent.context_compressor.last_prompt_tokens == 100
        assert agent.context_compressor.last_completion_tokens == 25
        assert agent.context_compressor.last_total_tokens == 130
        assert agent.context_compressor.context_length == 200000

    def test_run_turn_model_override_wired_from_account_mode(self, monkeypatch):
        """run_conversation must derive turn/start's model_override from the
        account-mode decision (codex_app_server_turn_model): BYO sends no
        model even when a polluted explicit model is lingering on the agent,
        platform sends the explicit model."""
        captured: list = []

        def fake_run_turn(self, user_input: str, **kwargs):
            captured.append(kwargs.get("model_override"))
            return TurnResult(
                final_text="done",
                projected_messages=[{"role": "assistant", "content": "done"}],
                turn_id="turn-model-1",
                thread_id="thread-model-1",
            )

        monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
        monkeypatch.setattr(
            CodexAppServerSession, "ensure_started", lambda self: "thread-model-1"
        )

        byo_agent = _make_codex_agent()
        byo_agent.codex_account_mode = "byo"
        byo_agent.codex_explicit_model = "glm-5.2"  # leftover must NOT leak
        with patch.object(byo_agent, "_spawn_background_review", return_value=None):
            byo_agent.run_conversation("hello")
        assert captured[-1] == ""

        platform_agent = _make_codex_agent()
        platform_agent.codex_account_mode = "platform"
        platform_agent.codex_explicit_model = "glm-5.2"
        with patch.object(platform_agent, "_spawn_background_review", return_value=None):
            platform_agent.run_conversation("hello")
        assert captured[-1] == "glm-5.2"

    def test_byo_usage_model_uses_codex_default_not_polluted_agent_model(self):
        from agent.codex_runtime import _record_codex_app_server_usage
        from tui_gateway.services.session_info import get_usage

        agent = _usage_agent(account_mode="byo", model="glm-5.2")
        result = _record_codex_app_server_usage(agent, _usage_turn())

        assert result["model"] == "gpt-5.5"
        assert agent.model == "gpt-5.5"
        assert get_usage(agent)["model"] == "gpt-5.5"
        assert agent._session_db.calls[-1]["model"] == "gpt-5.5"

    def test_platform_usage_model_uses_explicit_turn_start_model(self):
        from agent.codex_runtime import _record_codex_app_server_usage
        from tui_gateway.services.session_info import get_usage

        agent = _usage_agent(account_mode="platform", model="glm-5.2-polluted")
        agent.codex_explicit_model = "glm-5.2"
        result = _record_codex_app_server_usage(
            agent,
            _usage_turn(requested_model="glm-5.2"),
        )

        assert result["model"] == "glm-5.2"
        assert agent.model == "glm-5.2"
        assert get_usage(agent)["model"] == "glm-5.2"
        assert agent._session_db.calls[-1]["model"] == "glm-5.2"

    def test_projected_messages_are_spliced(self, fake_session):
        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hello")
        msgs = result["messages"]
        # User message + 3 projected (assistant tool_call + tool + assistant text)
        assert len(msgs) >= 4
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "hello"
        # Last assistant message has the final text
        final = [m for m in msgs if m.get("role") == "assistant"
                 and m.get("content") == "echo: hello"]
        assert final, f"expected final assistant message in {msgs}"

    def test_nudge_counters_tick(self, fake_session):
        """The skill nudge counter must accumulate tool_iterations across
        turns. The memory nudge counter is gated on memory being configured
        (which we skip via skip_memory=True), so we don't assert on it here —
        a separate test below covers that path explicitly."""
        agent = _make_codex_agent()
        agent._iters_since_skill = 0
        agent._user_turn_count = 0
        with patch.object(agent, "_spawn_background_review", return_value=None):
            agent.run_conversation("first")
        assert agent._iters_since_skill == 1  # one tool_iteration in fake turn
        # _user_turn_count is incremented by run_conversation pre-loop, not
        # by the codex helper — confirms we delegate that to the standard flow.
        assert agent._user_turn_count == 1
        with patch.object(agent, "_spawn_background_review", return_value=None):
            agent.run_conversation("second")
        assert agent._iters_since_skill == 2
        assert agent._user_turn_count == 2

    def test_user_message_not_duplicated(self, fake_session):
        """Regression guard: the user message must appear exactly once in
        the messages list. The standard run_conversation pre-loop appends
        it, and the codex helper must NOT append again."""
        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("ping unique 12345")
        user_count = sum(
            1 for m in result["messages"]
            if m.get("role") == "user" and m.get("content") == "ping unique 12345"
        )
        assert user_count == 1, f"user message appeared {user_count}× in {result['messages']}"

    def test_background_review_NOT_invoked_below_threshold(self, fake_session):
        """A single turn shouldn't trigger background review — counters
        haven't reached the nudge interval (default 10)."""
        agent = _make_codex_agent()
        agent._memory_nudge_interval = 10
        agent._skill_nudge_interval = 10
        agent._iters_since_skill = 0
        with patch.object(agent, "_spawn_background_review",
                          return_value=None) as spawn:
            agent.run_conversation("ping")
        # Below threshold → review should NOT fire (was a real bug:
        # the helper was calling _spawn_background_review() with no
        # args after every turn, which would crash with TypeError).
        assert not spawn.called

    def test_background_review_skill_trigger_fires_above_threshold(
        self, monkeypatch
    ):
        """When tool iterations cross the skill nudge interval, the
        background review fires with review_skills=True and the right
        messages_snapshot signature."""
        from agent.transports.codex_app_server_session import (
            CodexAppServerSession, TurnResult,
        )
        # Make the fake session report 10 tool iterations in one turn
        # (matching the default skill threshold).
        def fake_run_turn(self, user_input: str, **kwargs):
            return TurnResult(
                final_text=f"echo: {user_input}",
                projected_messages=[
                    {"role": "assistant", "content": f"echo: {user_input}"},
                ],
                tool_iterations=10,
                turn_id="t1", thread_id="th1",
            )
        monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
        monkeypatch.setattr(
            CodexAppServerSession, "ensure_started", lambda self: "th1"
        )

        agent = _make_codex_agent()
        agent._skill_nudge_interval = 10
        agent._iters_since_skill = 0
        # Make valid_tool_names include 'skill_manage' so the gate passes
        agent.valid_tool_names = set(getattr(agent, "valid_tool_names", set()))
        agent.valid_tool_names.add("skill_manage")

        with patch.object(agent, "_spawn_background_review",
                          return_value=None) as spawn:
            agent.run_conversation("do tool work")

        assert spawn.called, "skill threshold tripped but review didn't fire"
        # Verify the call signature matches what _spawn_background_review
        # actually expects — this is the regression guard for the original
        # bug where the codex path called it with no args at all.
        call = spawn.call_args
        assert "messages_snapshot" in call.kwargs
        assert isinstance(call.kwargs["messages_snapshot"], list)
        assert call.kwargs["review_skills"] is True
        # Counter should be reset after the review fires
        assert agent._iters_since_skill == 0

    def test_background_review_signature_never_breaks(self, fake_session):
        """Even when no trigger fires, the helper must never call
        _spawn_background_review with the wrong signature. Run a turn,
        then run another turn after manually tripping the skill counter
        and confirm the call shape is the kwargs-only form the function
        actually accepts."""
        agent = _make_codex_agent()
        agent._skill_nudge_interval = 1  # very low so any iter trips it
        agent._iters_since_skill = 0
        agent.valid_tool_names = set(getattr(agent, "valid_tool_names", set()))
        agent.valid_tool_names.add("skill_manage")

        with patch.object(agent, "_spawn_background_review",
                          return_value=None) as spawn:
            agent.run_conversation("first")
        # The fake session reports tool_iterations=1, which trips
        # _skill_nudge_interval=1. So review should fire.
        assert spawn.called
        # Critical invariant: positional args must be empty, all real
        # args must be kwargs (matching _spawn_background_review's
        # actual signature).
        call = spawn.call_args
        assert call.args == (), (
            f"expected no positional args, got {call.args!r} — "
            "would crash _spawn_background_review at runtime"
        )
        assert "messages_snapshot" in call.kwargs

    def test_chat_completions_loop_is_not_entered(self, fake_session):
        """The early-return must bypass the regular API call loop entirely.
        We confirm by patching the SDK call and asserting it's never invoked."""
        agent = _make_codex_agent()
        # The chat_completions loop calls self.client.chat.completions.create(...)
        # If our early-return works, that path is dead.
        with patch.object(agent, "client") as client_mock, patch.object(
            agent, "_spawn_background_review", return_value=None
        ):
            agent.run_conversation("hi")
        assert not client_mock.chat.completions.create.called

    def test_gateway_terminal_cwd_seeds_codex_thread_cwd(self, monkeypatch, tmp_path):
        """Gateway sessions set TERMINAL_CWD without stamping agent.session_cwd.
        Codex app-server must still start in that configured workspace instead
        of falling back to the Hermes daemon process cwd."""
        from agent.transports.codex_app_server_session import (
            CodexAppServerSession, TurnResult,
        )

        captured: dict[str, str] = {}

        def fake_init(self, **kwargs):
            captured["cwd"] = kwargs["cwd"]
            self._thread_id = "thread-stub-1"

        def fake_run_turn(self, user_input: str, **kwargs):
            return TurnResult(
                final_text="ok",
                projected_messages=[{"role": "assistant", "content": "ok"}],
                turn_id="turn-stub-1",
                thread_id="thread-stub-1",
            )

        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        monkeypatch.setattr(CodexAppServerSession, "__init__", fake_init)
        monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))

        agent = _make_codex_agent()
        assert not hasattr(agent, "session_cwd")
        with patch.object(agent, "_spawn_background_review", return_value=None):
            agent.run_conversation("hi")

        assert captured["cwd"] == str(tmp_path)

    def test_agent_codex_home_seeds_codex_session(self, monkeypatch, tmp_path):
        from agent.transports.codex_app_server_session import (
            CodexAppServerSession, TurnResult,
        )

        captured: dict[str, str | None] = {}

        def fake_init(self, **kwargs):
            captured["codex_home"] = kwargs.get("codex_home")
            self._thread_id = "thread-stub-1"

        def fake_run_turn(self, user_input: str, **kwargs):
            return TurnResult(
                final_text="ok",
                projected_messages=[{"role": "assistant", "content": "ok"}],
                turn_id="turn-stub-1",
                thread_id="thread-stub-1",
            )

        monkeypatch.setattr(CodexAppServerSession, "__init__", fake_init)
        monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))

        with patch("hermes_logging.setup_logging"):
            agent = _make_codex_agent()
        agent.codex_home = str(tmp_path / "codex-home")
        with patch.object(agent, "_spawn_background_review", return_value=None):
            agent.run_conversation("hi")

        assert captured["codex_home"] == str(tmp_path / "codex-home")


class TestTeamMemberCodexContextInjection:
    def test_member_chat_codex_context_excludes_own_member_messages(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
        agent, db = _make_team_codex_agent(tmp_path)
        session_id = agent.session_id
        _set_member_chat_dovie_context(monkeypatch, session_id=session_id)
        captured = _install_capturing_codex(monkeypatch)

        _append_team_context_message(
            db,
            session_id,
            content="Leader context survives",
            participant_id="leader:team-1",
        )
        _append_team_context_message(
            db,
            session_id,
            content="Designer context survives",
            participant_id="member:designer",
        )
        _append_team_context_message(
            db,
            session_id,
            content="SELF MESSAGE MUST NOT BE IN PREFACE",
            participant_id="member:codex-member",
        )
        _append_team_context_message(
            db,
            session_id,
            role="user",
            content="请总结一下",
            participant_id="",
        )

        _run_codex_turn_no_persist(agent, "请总结一下")

        assert captured, "codex turn was not submitted"
        turn_input = captured[-1]
        assert turn_input.startswith("[团队会话背景")
        assert "小多 (Leader): Leader context survives" in turn_input
        assert "UI/UX设计师: Designer context survives" in turn_input
        assert "SELF MESSAGE MUST NOT BE IN PREFACE" not in turn_input
        assert turn_input.count("请总结一下") == 1
        assert turn_input.endswith("\n\n请总结一下")

    def test_member_chat_codex_turn_start_failure_does_not_advance_watermark(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
        agent, db = _make_team_codex_agent(tmp_path)
        session_id = agent.session_id
        _set_member_chat_dovie_context(monkeypatch, session_id=session_id)
        captured = _install_capturing_codex(
            monkeypatch,
            turns=[
                TurnResult(error="turn/start failed", thread_id="thread-ok"),
                TurnResult(
                    final_text="done",
                    projected_messages=[{"role": "assistant", "content": "done"}],
                    turn_id="turn-ok-2",
                    thread_id="thread-ok",
                ),
            ],
        )
        _append_team_context_message(
            db,
            session_id,
            content="retry-visible-context",
            participant_id="leader:team-1",
        )

        _run_codex_turn_no_persist(agent, "第一次")
        watermark_path = _context_watermark_file(agent)
        assert not watermark_path.exists(), "turn/start failure must not write watermark"

        _run_codex_turn_no_persist(agent, "第二次")

        assert len(captured) == 2
        assert "retry-visible-context" in captured[0]
        assert "retry-visible-context" in captured[1]
        assert watermark_path.exists(), "successful turn/start should persist watermark"

    def test_member_chat_codex_first_backlog_truncates_at_limit(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
        agent, db = _make_team_codex_agent(tmp_path)
        session_id = agent.session_id
        _set_member_chat_dovie_context(monkeypatch, session_id=session_id)
        captured = _install_capturing_codex(monkeypatch)

        for index in range(31):
            _append_team_context_message(
                db,
                session_id,
                content=f"history-{index:02d}",
                participant_id="leader:team-1",
                conversation_message_id=f"history-{index:02d}",
            )

        _run_codex_turn_no_persist(agent, "看历史")

        turn_input = captured[-1]
        assert "更早历史已省略" in turn_input
        assert "history-00" not in turn_input
        assert "history-01" in turn_input
        assert "history-30" in turn_input

    def test_direct_codex_turn_input_is_unchanged_without_member_context(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
        monkeypatch.delenv("HERMES_DOVIE_PRODUCT_CONTEXT", raising=False)
        with patch("hermes_logging.setup_logging"):
            agent = _make_codex_agent()
        agent.codex_home = str(tmp_path / "codex-home")
        captured = _install_capturing_codex(monkeypatch)

        original = "plain direct codex message"
        _run_codex_turn_no_persist(agent, original)

        assert captured == [original]
        assert not _context_watermark_file(agent).exists()

    def test_member_chat_codex_thread_rebuild_clears_watermark_and_resends_backlog(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
        agent, db = _make_team_codex_agent(tmp_path)
        session_id = agent.session_id
        _set_member_chat_dovie_context(monkeypatch, session_id=session_id)
        seq = _append_team_context_message(
            db,
            session_id,
            content="backlog-after-thread-rebuild",
            participant_id="leader:team-1",
        )
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir(parents=True)
        (codex_home / "hermes_thread_map.json").write_text(
            json.dumps({session_id: "old-thread"}),
            encoding="utf-8",
        )
        watermark_path = _context_watermark_file(agent)
        watermark_path.write_text(
            json.dumps({f"{session_id}::member:codex-member": seq}),
            encoding="utf-8",
        )

        def ensure_started_with_rebuild(self):
            self._thread_id = "new-thread"
            self._thread_rebuilt_from_prior = True
            return "new-thread"

        captured = _install_capturing_codex(
            monkeypatch,
            ensure_started=ensure_started_with_rebuild,
        )

        _run_codex_turn_no_persist(agent, "线程重建后继续")

        assert "backlog-after-thread-rebuild" in captured[-1]
        watermark_data = json.loads(watermark_path.read_text(encoding="utf-8"))
        assert watermark_data[f"{session_id}::member:codex-member"] >= seq


class TestReviewForkApiModeDowngrade:
    """When the parent agent runs on codex_app_server, the background
    review fork must downgrade to codex_responses — otherwise the fork
    can't dispatch agent-loop tools (memory, skill_manage) which is the
    whole point of the review."""

    def test_codex_app_server_parent_downgrades_review_fork(self):
        """Live test against the real _spawn_background_review code path:
        verify the review_agent gets api_mode=codex_responses when the
        parent is codex_app_server."""
        from unittest.mock import MagicMock, patch as _patch
        agent = _make_codex_agent()
        # Pretend memory + skills are configured so the review fork
        # reaches the AIAgent constructor.
        agent._memory_store = MagicMock()
        agent._memory_enabled = True
        agent._user_profile_enabled = True
        # Mock _current_main_runtime to return the parent's codex_app_server
        # state so we can confirm the helper detects + downgrades it.
        agent._current_main_runtime = lambda: {
            "api_mode": "codex_app_server",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "stub-token",
        }
        # Capture what AIAgent gets constructed with inside the helper.
        captured = {}

        def _capture_init(self, **kwargs):
            captured.update(kwargs)
            # Set bare attributes the rest of the spawn function reads
            # so it can finish without exploding.
            self.api_mode = kwargs.get("api_mode")
            self.provider = kwargs.get("provider")
            self.model = kwargs.get("model")
            self._memory_write_origin = None
            self._memory_write_context = None
            self._memory_store = None
            self._memory_enabled = False
            self._user_profile_enabled = False
            self._memory_nudge_interval = 0
            self._skill_nudge_interval = 0
            self.suppress_status_output = False
            self._session_messages = []

            def _no_op_run_conv(*a, **kw):
                return {"final_response": "", "messages": []}
            self.run_conversation = _no_op_run_conv

            def _no_op_close(*a, **kw):
                return None
            self.close = _no_op_close

        with _patch("run_agent.AIAgent.__init__", _capture_init):
            agent._spawn_background_review(
                messages_snapshot=[{"role": "user", "content": "x"}],
                review_memory=True,
                review_skills=False,
            )
            # Wait for the spawned thread to actually execute
            import time
            for _ in range(30):
                if "api_mode" in captured:
                    break
                time.sleep(0.1)

        assert captured.get("api_mode") == "codex_responses", (
            f"review fork should be downgraded to codex_responses when "
            f"parent is codex_app_server; got {captured.get('api_mode')!r}"
        )


class TestErrorHandling:
    def test_session_exception_returns_partial_with_error(self, monkeypatch):
        def boom_run_turn(self, user_input, **kwargs):
            raise RuntimeError("subprocess died")

        monkeypatch.setattr(CodexAppServerSession, "ensure_started",
                            lambda self: "t1")
        monkeypatch.setattr(CodexAppServerSession, "run_turn", boom_run_turn)

        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hi")
        assert result["completed"] is False
        assert result["partial"] is True
        assert "subprocess died" in result["error"]
        assert "codex-runtime auto" in result["final_response"]

    def test_interrupted_turn_marked_partial(self, monkeypatch):
        def interrupted_turn(self, user_input, **kwargs):
            return TurnResult(
                final_text="",
                projected_messages=[],
                tool_iterations=0,
                interrupted=True,
                error="user interrupted",
                turn_id="t",
                thread_id="th",
            )
        monkeypatch.setattr(CodexAppServerSession, "ensure_started",
                            lambda self: "th")
        monkeypatch.setattr(CodexAppServerSession, "run_turn", interrupted_turn)

        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hi")
        assert result["completed"] is False
        assert result["partial"] is True
        assert result["error"] == "user interrupted"


class TestSessionRetirementOnRunAgent:
    """run_agent.py side: when run_turn returns should_retire=True, the
    AIAgent must close + null _codex_session so the next turn respawns."""

    def test_should_retire_drops_session(self, monkeypatch):
        closes = {"count": 0}

        def fake_run_turn(self, user_input, **kwargs):
            return TurnResult(
                final_text="",
                projected_messages=[],
                tool_iterations=0,
                interrupted=True,
                error="turn timed out after 600.0s",
                turn_id="tu1",
                thread_id="th1",
                should_retire=True,
            )

        def fake_close(self):
            closes["count"] += 1

        monkeypatch.setattr(CodexAppServerSession, "ensure_started",
                            lambda self: "th1")
        monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
        monkeypatch.setattr(CodexAppServerSession, "close", fake_close)

        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hi")

        # The session was closed and cleared
        assert closes["count"] == 1
        assert getattr(agent, "_codex_session", "MISSING") is None
        # Partial result was still returned (caller still sees the error)
        assert result["partial"] is True
        assert result["error"] == "turn timed out after 600.0s"

    def test_normal_turn_keeps_session(self, fake_session):
        """fake_session fixture returns should_retire=False (default).
        The session must stay attached for the next turn to reuse."""
        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            agent.run_conversation("hi")
        # Session was lazily created and still attached.
        assert getattr(agent, "_codex_session", None) is not None

    def test_exception_path_also_drops_session(self, monkeypatch):
        """Even if run_turn raises (not just sets should_retire), we must
        drop the session — a thrown exception is the strongest possible
        signal the process is dead."""
        closes = {"count": 0}

        def boom_run_turn(self, user_input, **kwargs):
            raise RuntimeError("codex segfaulted")

        def fake_close(self):
            closes["count"] += 1

        monkeypatch.setattr(CodexAppServerSession, "ensure_started",
                            lambda self: "th1")
        monkeypatch.setattr(CodexAppServerSession, "run_turn", boom_run_turn)
        monkeypatch.setattr(CodexAppServerSession, "close", fake_close)

        agent = _make_codex_agent()
        with patch.object(agent, "_spawn_background_review", return_value=None):
            result = agent.run_conversation("hi")

        assert closes["count"] == 1
        assert agent._codex_session is None
        assert result["completed"] is False
        assert "codex segfaulted" in result["error"]
