from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_agent.composition.cli_session_store import open_cli_session_store
from run_agent import AIAgent


def _tool_def(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _tool_call(name: str, arguments: dict, call_id: str):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _response(content: str, *, tool_call=None):
    message = SimpleNamespace(
        content=content,
        tool_calls=[tool_call] if tool_call is not None else None,
    )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=message,
                finish_reason="tool_calls" if tool_call is not None else "stop",
            )
        ],
        model="test/model",
        usage=None,
    )


def test_unverified_final_gets_one_ephemeral_provider_continuation(tmp_path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}', encoding="utf-8"
    )
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'", encoding="utf-8")
    store = open_cli_session_store(tmp_path / "state.db")

    tools = [_tool_def("write_file"), _tool_def("terminal")]
    with (
        patch("run_agent.get_tool_definitions", return_value=tools),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=store,
            session_id="conversation-1",
            cwd=str(root),
            max_iterations=4,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.client.chat.completions.create.side_effect = [
        _response(
            "",
            tool_call=_tool_call(
                "write_file",
                {"path": str(root / "src/app.ts"), "content": "export {}"},
                "write-1",
            ),
        ),
        _response("Implemented without running tests."),
        _response(
            "",
            tool_call=_tool_call(
                "terminal",
                {"command": "pnpm test", "workdir": str(root)},
                "test-1",
            ),
        ),
        _response("Implemented and verified."),
    ]

    def execute(name, *_args, **_kwargs):
        if name == "write_file":
            return json.dumps({"bytes_written": 9})
        if name == "terminal":
            return json.dumps({"output": "all passed", "exit_code": 0})
        raise AssertionError(name)

    with (
        patch("run_agent.handle_function_call", side_effect=execute),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("Implement the feature")

    assert result["final_response"] == "Implemented and verified."
    assert result["api_calls"] == 4
    assert agent.client.chat.completions.create.call_count == 4
    third_request = agent.client.chat.completions.create.call_args_list[2].kwargs
    provider_user = next(
        message
        for message in third_request["messages"]
        if message.get("role") == "user"
    )
    assert "hermes_runtime_context" in provider_user["content"]
    assert "Verification is required" in provider_user["content"]

    durable_text = "\n".join(
        str(message.get("content") or "") for message in result["messages"]
    )
    assert "hermes_runtime_context" not in durable_text
    assert "Implemented without running tests." not in durable_text
    status = store.verification.status("conversation-1", root)
    assert status is not None
    assert status.status == "passed"
    store.close()


def test_guard_is_bounded_when_model_still_does_not_verify(tmp_path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}', encoding="utf-8"
    )
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'", encoding="utf-8")
    store = open_cli_session_store(tmp_path / "state.db")
    tools = [_tool_def("write_file")]
    with (
        patch("run_agent.get_tool_definitions", return_value=tools),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=store,
            session_id="conversation-1",
            cwd=str(root),
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.client.chat.completions.create.side_effect = [
        _response(
            "",
            tool_call=_tool_call(
                "write_file",
                {"path": str(root / "src/app.ts"), "content": "export {}"},
                "write-1",
            ),
        ),
        _response("First premature completion."),
        _response("Bounded completion without evidence."),
    ]

    with (
        patch(
            "run_agent.handle_function_call",
            return_value=json.dumps({"bytes_written": 9}),
        ),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("Implement the feature")

    assert result["final_response"] == "Bounded completion without evidence."
    assert agent.client.chat.completions.create.call_count == 3
    assert store.verification.status("conversation-1", root).status == "unverified"
    store.close()


def test_codex_app_server_continuation_keeps_internal_requirement_out_of_transcript(
    tmp_path,
) -> None:
    from agent.codex_runtime import run_codex_app_server_turn
    from agent.transports.codex_app_server_session import TurnResult

    root = tmp_path / "project"
    root.mkdir()
    (root / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}', encoding="utf-8"
    )
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'", encoding="utf-8")
    store = open_cli_session_store(tmp_path / "state.db")
    store.verification.mark_edited("conversation-1", root, ["src/app.ts"])

    class FakeCodexSession:
        def __init__(self) -> None:
            self.inputs: list[str] = []

        def run_turn(self, *, user_input, model_override=""):
            self.inputs.append(user_input)
            if len(self.inputs) == 1:
                return TurnResult(
                    final_text="Premature Codex completion.",
                    thread_id="thread-1",
                    turn_id="turn-1",
                    tool_iterations=1,
                    projected_messages=[
                        {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [{"id": "file-1"}],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "file-1",
                            "content": "applied",
                        },
                        {"role": "assistant", "content": "Premature Codex completion."},
                    ],
                )
            store.verification.record_terminal(
                "conversation-1",
                command="pnpm test",
                cwd=root,
                exit_code=0,
                output="passed",
            )
            return TurnResult(
                final_text="Verified Codex completion.",
                thread_id="thread-1",
                turn_id="turn-2",
                projected_messages=[
                    {"role": "assistant", "content": "Verified Codex completion."}
                ],
            )

        def close(self) -> None:
            pass

    codex_session = FakeCodexSession()
    persisted = []
    agent = SimpleNamespace(
        session_id="conversation-1",
        memory_session_id="conversation-1",
        session_cwd=str(root),
        _session_db=store,
        _codex_session=codex_session,
        verification_completion_guard=True,
        verification_max_attempts=1,
        _visible_transcript_session_id=lambda: "conversation-1",
        _active_run_context=lambda: None,
        _emit_status=lambda _message: None,
        _persist_session=lambda messages: persisted.append(list(messages)),
        _sync_external_memory_for_turn=lambda **_kwargs: None,
        _spawn_background_review=lambda **_kwargs: None,
        _iters_since_skill=0,
        _skill_nudge_interval=0,
        valid_tool_names=set(),
        codex_account_mode="byo",
        codex_extra_env=None,
        codex_home=None,
        platform="desktop",
    )
    messages = [{"role": "user", "content": "Implement the feature"}]
    with (
        patch("agent.codex_runtime._record_codex_app_server_usage", return_value={}),
        patch("agent.codex_runtime._record_codex_app_server_compaction", return_value=False),
    ):
        result = run_codex_app_server_turn(
            agent,
            user_message="Implement the feature",
            original_user_message="Implement the feature",
            messages=messages,
            effective_task_id="task-1",
        )

    assert result["final_response"] == "Verified Codex completion."
    assert result["api_calls"] == 2
    assert len(codex_session.inputs) == 2
    assert "Verification is required" in codex_session.inputs[1]
    durable_text = "\n".join(
        str(message.get("content") or "") for message in result["messages"]
    )
    assert "Verification is required" not in durable_text
    assert "Premature Codex completion." not in durable_text
    assert "Verified Codex completion." in durable_text
    assert persisted
    store.close()


def test_budget_exhaustion_preserves_only_the_gated_answer(tmp_path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}', encoding="utf-8"
    )
    (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'", encoding="utf-8")
    store = open_cli_session_store(tmp_path / "state.db")
    tools = [_tool_def("write_file"), _tool_def("terminal")]
    with (
        patch("run_agent.get_tool_definitions", return_value=tools),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=store,
            session_id="conversation-1",
            cwd=str(root),
            max_iterations=2,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.client.chat.completions.create.side_effect = [
        _response(
            "",
            tool_call=_tool_call(
                "write_file",
                {"path": str(root / "src/app.ts"), "content": "export {}"},
                "write-1",
            ),
        ),
        _response("Useful but not yet verified."),
        _response(
            "",
            tool_call=_tool_call(
                "terminal", {"command": "git status", "workdir": str(root)}, "git-1"
            ),
        ),
        _response(
            "",
            tool_call=_tool_call(
                "terminal", {"command": "git diff", "workdir": str(root)}, "git-2"
            ),
        ),
    ]

    def execute(name, *_args, **_kwargs):
        if name == "write_file":
            return json.dumps({"bytes_written": 9})
        return json.dumps({"output": "ok", "exit_code": 0})

    with (
        patch("run_agent.handle_function_call", side_effect=execute),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(
            agent,
            "_handle_max_iterations",
            side_effect=AssertionError("must not replace the held answer"),
        ),
    ):
        result = agent.run_conversation("Implement the feature")

    assert result["final_response"] == "Useful but not yet verified."
    assert result["completed"] is False
    assert result["turn_exit_reason"] == "max_iterations_reached(3/2)"
    assert [
        message.get("content")
        for message in result["messages"]
        if message.get("role") == "assistant"
        and message.get("content") == "Useful but not yet verified."
    ] == ["Useful but not yet verified."]
    store.close()
