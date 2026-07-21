from types import SimpleNamespace
from typing import Any

from agent.turn_finalizer import finalize_turn


class _RecordingAgent:
    def __init__(self) -> None:
        self.max_iterations = 90
        self.iteration_budget = SimpleNamespace(remaining=10, used=1, max_total=90)
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self.quiet_mode = True
        self.model = "test-model"
        self.provider = "test-provider"
        self.base_url = ""
        self.session_id = "sess-test"
        self.platform = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None
        self._response_was_previewed = False
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names: list[str] = []
        self.persisted_messages: list[dict[str, Any]] | None = None
        self.rewritten_message: dict[str, Any] | None = None
        for attr in (
            "session_input_tokens",
            "session_output_tokens",
            "session_cache_read_tokens",
            "session_cache_write_tokens",
            "session_reasoning_tokens",
            "session_prompt_tokens",
            "session_completion_tokens",
            "session_total_tokens",
            "session_estimated_cost_usd",
        ):
            setattr(self, attr, 0)
        self.session_cost_status = "unknown"
        self.session_cost_source = "test"

    def _save_trajectory(self, *_args, **_kwargs) -> None:
        pass

    def _cleanup_task_resources(self, *_args, **_kwargs) -> None:
        pass

    def _drop_trailing_empty_response_scaffolding(self, _messages) -> None:
        pass

    def _persist_session(self, messages, _conversation_history) -> None:
        self.persisted_messages = [dict(message) for message in messages]

    def _rewrite_persisted_message_content(self, message) -> bool:
        self.rewritten_message = dict(message)
        return True

    def _file_mutation_verifier_enabled(self) -> bool:
        return False

    def _turn_completion_explainer_enabled(self) -> bool:
        return False

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self) -> None:
        pass

    def _sync_external_memory_for_turn(self, **_kwargs) -> None:
        pass


def _finalize(agent: _RecordingAgent, messages: list[dict[str, Any]], response: str):
    return finalize_turn(
        agent,
        final_response=response,
        api_call_count=3,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="question",
        original_user_message="question",
        _should_review_memory=False,
        _turn_exit_reason="text_response(final)",
    )


def test_final_response_closes_non_assistant_tail(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _RecordingAgent()
    messages = [
        {"role": "user", "content": "question"},
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
    ]

    result = _finalize(agent, messages, "Done.")

    assert result["messages"][-1] == {"role": "assistant", "content": "Done."}
    assert agent.persisted_messages is not None
    assert agent.persisted_messages[-1] == {"role": "assistant", "content": "Done."}


def test_final_response_fills_multimodal_pure_tool_call_tail(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _RecordingAgent()
    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [{"type": "image_url", "image_url": {"url": "data:"}}],
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "inspect", "arguments": "{}"},
                }
            ],
        },
    ]

    _finalize(agent, messages, "Recovered answer.")

    assert agent.persisted_messages is not None
    assert agent.persisted_messages[-1]["content"] == "Recovered answer."
    assert agent.persisted_messages[-1]["tool_calls"]
    assert agent.rewritten_message is not None
    assert agent.rewritten_message["content"] == "Recovered answer."


def test_final_response_does_not_clobber_textual_tool_call_tail(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = _RecordingAgent()
    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Existing text."}],
            "tool_calls": [{"id": "call-1", "function": {"name": "inspect"}}],
        },
    ]

    _finalize(agent, messages, "Different answer.")

    assert agent.persisted_messages is not None
    assert agent.persisted_messages[-1]["content"] == [
        {"type": "output_text", "text": "Existing text."}
    ]
    assert agent.rewritten_message is None


def test_tool_call_tail_rewrite_is_durable_across_reload(tmp_path):
    from hermes_agent.composition.cli_session_store import open_cli_session_store
    from run_agent import AIAgent

    db = open_cli_session_store(db_path=tmp_path / "sessions.db")
    agent = object.__new__(AIAgent)
    agent.session_id = "durable-final-response"
    agent.platform = "test"
    agent.model = "test/model"
    agent._session_db = db
    agent._session_db_created = False
    agent._session_init_model_config = None
    agent._cached_system_prompt = None
    agent._parent_session_id = None
    agent._last_flushed_db_idx = 0
    agent._persist_user_message_idx = None
    agent._persist_user_message_override = None
    agent._hermes_active_run_id = "run-1"
    agent._hermes_active_turn_id = "turn-1"
    agent._hermes_active_runtime_scope_key = ""
    agent.run_context = None
    agent._run_context = None
    agent._ensure_db_session()

    messages = [
        {
            "role": "user",
            "content": "question",
            "metadata": {"run_id": "run-1", "turn_id": "turn-1"},
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "inspect", "arguments": "{}"},
                }
            ],
        },
    ]
    agent._persist_session(messages, [])
    assert db.messages.list(agent.session_id)[-1]["content"] == ""

    messages[-1]["content"] = "Recovered answer."
    agent._persist_session(messages, [])
    assert db.messages.list(agent.session_id)[-1]["content"] == ""

    assert agent._rewrite_persisted_message_content(messages[-1]) is True
    reloaded = db.messages.all_as_conversation(agent.session_id)
    assert reloaded[-1]["content"] == "Recovered answer."
    assert reloaded[-1]["tool_calls"]
