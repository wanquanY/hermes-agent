from __future__ import annotations

import json
from types import SimpleNamespace

from agent.dovie_attribution import build_dovie_attribution_headers
from channels.session_context import clear_session_vars, set_session_vars
from tools import delegate_tool as dt


def _context() -> dict:
    return {
        "cloud_query": {
            "query_id": "query-delegate",
            "root_query_id": "root-query-delegate",
            "agent_run_id": "root-run-delegate",
            "query_context_token": "token-delegate",
        },
        "sourceAgentProfileId": "profile-root",
    }


def test_prepare_child_dovie_attribution_generates_child_headers() -> None:
    child = SimpleNamespace(
        profile_id="profile-child",
        request_overrides={"extra_headers": {"X-Existing": "keep"}},
    )
    tokens = set_session_vars(dovie_product_context=json.dumps(_context()))
    try:
        prepared = dt._prepare_child_dovie_attribution(child)
        root_headers = build_dovie_attribution_headers()
    finally:
        clear_session_vars(tokens)

    assert prepared is True
    assert child._dovie_child_executing_agent_profile_id == "profile-child"
    assert child._dovie_child_agent_role == "subagent"
    headers = child.request_overrides["extra_headers"]
    assert headers["X-Existing"] == "keep"
    assert {
        key
        for key in headers
        if key.startswith("X-Dovie")
    } == {
        "X-Dovie-Executing-Agent-Profile-Id",
        "X-Dovie-Agent-Role",
    }
    assert headers["X-Dovie-Executing-Agent-Profile-Id"] == "profile-child"
    assert headers["X-Dovie-Agent-Role"] == "subagent"
    assert root_headers["X-Dovie-Agent-Run-Id"] == "root-run-delegate"


def test_child_conversation_overlay_restores_after_delegate_run() -> None:
    class Child:
        _dovie_child_executing_agent_profile_id = "profile-child"
        _dovie_child_agent_role = "subagent"

        def run_conversation(self, *, user_message: str, task_id: str):
            headers = build_dovie_attribution_headers()
            return {
                "user_message": user_message,
                "task_id": task_id,
                "headers": headers,
            }

    tokens = set_session_vars(dovie_product_context=json.dumps(_context()))
    try:
        result = dt._run_child_conversation_with_dovie_attribution(
            Child(),
            goal="do work",
            child_task_id="child-task",
        )
        restored_headers = build_dovie_attribution_headers()
    finally:
        clear_session_vars(tokens)

    assert result["user_message"] == "do work"
    assert result["task_id"] == "child-task"
    assert result["headers"]["X-Dovie-Agent-Run-Id"] == "root-run-delegate"
    assert result["headers"]["X-Dovie-Executing-Agent-Profile-Id"] == "profile-child"
    assert result["headers"]["X-Dovie-Agent-Role"] == "subagent"
    assert restored_headers["X-Dovie-Agent-Run-Id"] == "root-run-delegate"
    assert restored_headers["X-Dovie-Executing-Agent-Profile-Id"] == "profile-root"
    assert "X-Dovie-Agent-Role" not in restored_headers


def test_anonymous_child_dovie_attribution_has_no_overlay_headers() -> None:
    child = SimpleNamespace(request_overrides={"extra_headers": {"X-Existing": "keep"}})

    tokens = set_session_vars(dovie_product_context=json.dumps(_context()))
    try:
        prepared = dt._prepare_child_dovie_attribution(child)
        root_headers = build_dovie_attribution_headers()
    finally:
        clear_session_vars(tokens)

    assert prepared is False
    headers = child.request_overrides["extra_headers"]
    assert headers == {"X-Existing": "keep"}
    assert not hasattr(child, "_dovie_child_executing_agent_profile_id")
    assert root_headers["X-Dovie-Agent-Run-Id"] == "root-run-delegate"
