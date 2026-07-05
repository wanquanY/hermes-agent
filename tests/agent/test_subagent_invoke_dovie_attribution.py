from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent.subagent_invoke import invoke_subagent
from gateway.session_context import clear_session_vars, set_session_vars


class _FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="done", tool_calls=[]),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
            ),
        )


class _FakeClient:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions())


def _agent(client: _FakeClient):
    return SimpleNamespace(
        client=client,
        model="test-model",
        provider="test",
        api_mode="chat_completions",
        request_overrides={},
        valid_tool_names=set(),
    )


def _make_profile(root: Path, profile_id: str) -> None:
    home = root / "profiles" / profile_id
    home.mkdir(parents=True)
    (home / "SOUL.md").write_text("Target soul", encoding="utf-8")


def test_invoke_subagent_adds_dovie_child_headers(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _make_profile(tmp_path, "profile-worker")
    client = _FakeClient()
    tokens = set_session_vars(
        dovie_product_context=json.dumps(
            {
                "cloud_query": {
                    "query_id": "query-invoke",
                    "root_query_id": "root-query-invoke",
                    "agent_run_id": "root-run-invoke",
                    "query_context_token": "token-invoke",
                },
                "sourceAgentProfileId": "profile-root",
            }
        )
    )
    try:
        result = invoke_subagent(_agent(client), "profile-worker", "do work")
    finally:
        clear_session_vars(tokens)

    assert result.output == "done"
    headers = client.chat.completions.calls[0]["extra_headers"]
    assert "X-Dovie-Agent-Run-Id" not in headers
    assert "X-Dovie-Query-Id" not in headers
    assert headers["X-Dovie-Executing-Agent-Profile-Id"] == "profile-worker"
    assert headers["X-Dovie-Agent-Role"] == "subagent"
