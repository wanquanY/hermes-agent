from __future__ import annotations

import json

import httpx
import pytest

from agent.dovie_attribution import (
    attach_dovie_attribution_request_hook,
    build_dovie_attribution_headers,
    dovie_attribution_request_hook,
)
from gateway.session_context import clear_session_vars, set_session_vars


def _context(query_id: str = "query-1") -> dict:
    return {
        "cloud_query": {
            "query_id": query_id,
            "root_query_id": "root-query-1",
            "agent_run_id": "agent-run-1",
            "query_context_token": "context-token-1",
        },
        "sourceAgentProfileId": "profile-root-1",
        "sourceSessionId": "conversation-1",
        "sourceRunId": "run-1",
        "sourceTurnId": "turn-1",
        "sourceClientMessageId": "client-message-1",
    }


def _set_dovie_context(payload: dict | str):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return set_session_vars(dovie_product_context=raw)


@pytest.fixture(autouse=True)
def _clear_dovie_context(monkeypatch):
    monkeypatch.delenv("HERMES_DOVIE_PRODUCT_CONTEXT", raising=False)
    tokens = set_session_vars(dovie_product_context="")
    clear_session_vars(tokens)
    yield
    tokens = set_session_vars(dovie_product_context="")
    clear_session_vars(tokens)


def test_build_headers_maps_full_context_to_all_dovie_headers():
    tokens = _set_dovie_context(_context())
    try:
        assert build_dovie_attribution_headers() == {
            "X-Dovie-Query-Id": "query-1",
            "X-Dovie-Root-Query-Id": "root-query-1",
            "X-Dovie-Agent-Run-Id": "agent-run-1",
            "X-Dovie-Query-Context-Token": "context-token-1",
            "X-Dovie-Root-Agent-Profile-Id": "profile-root-1",
            "X-Dovie-Executing-Agent-Profile-Id": "profile-root-1",
            "X-Dovie-Conversation-Session-Id": "conversation-1",
            "X-Dovie-Run-Id": "run-1",
            "X-Dovie-Turn-Id": "turn-1",
            "X-Dovie-Client-Message-Id": "client-message-1",
        }
    finally:
        clear_session_vars(tokens)


def test_build_headers_uses_explicit_root_executing_and_agent_role():
    payload = {
        **_context(),
        "root_agent_profile_id": "profile-root-team",
        "executing_agent_profile_id": "profile-member-1",
        "agent_role": "team_member",
    }
    tokens = _set_dovie_context(payload)
    try:
        headers = build_dovie_attribution_headers()
    finally:
        clear_session_vars(tokens)

    assert headers["X-Dovie-Root-Agent-Profile-Id"] == "profile-root-team"
    assert headers["X-Dovie-Executing-Agent-Profile-Id"] == "profile-member-1"
    assert headers["X-Dovie-Agent-Role"] == "team_member"


def test_build_headers_executing_profile_falls_back_to_source_profile():
    payload = {
        **_context(),
        "root_agent_profile_id": "profile-root-team",
    }
    tokens = _set_dovie_context(payload)
    try:
        headers = build_dovie_attribution_headers()
    finally:
        clear_session_vars(tokens)

    assert headers["X-Dovie-Root-Agent-Profile-Id"] == "profile-root-team"
    assert headers["X-Dovie-Executing-Agent-Profile-Id"] == "profile-root-1"
    assert "X-Dovie-Agent-Role" not in headers


def test_build_headers_accepts_snake_case_top_level_ids():
    tokens = _set_dovie_context(
        {
            "cloud_query": {},
            "source_agent_profile_id": "profile-snake",
            "conversation_session_id": "conversation-snake",
            "source_run_id": "run-snake",
            "source_turn_id": "turn-snake",
            "source_client_message_id": "client-message-snake",
        }
    )
    try:
        headers = build_dovie_attribution_headers()
    finally:
        clear_session_vars(tokens)

    assert headers["X-Dovie-Root-Agent-Profile-Id"] == "profile-snake"
    assert headers["X-Dovie-Executing-Agent-Profile-Id"] == "profile-snake"
    assert headers["X-Dovie-Conversation-Session-Id"] == "conversation-snake"
    assert headers["X-Dovie-Run-Id"] == "run-snake"
    assert headers["X-Dovie-Turn-Id"] == "turn-snake"
    assert headers["X-Dovie-Client-Message-Id"] == "client-message-snake"


def test_build_headers_empty_or_bad_json_returns_empty_dict():
    tokens = _set_dovie_context("")
    try:
        assert build_dovie_attribution_headers() == {}
    finally:
        clear_session_vars(tokens)

    tokens = _set_dovie_context("{bad json")
    try:
        assert build_dovie_attribution_headers() == {}
    finally:
        clear_session_vars(tokens)


def test_build_headers_sanitizes_newlines_and_caps_values():
    # 上限 = 2048;600 个 x 触发不了截断,拉到 2500 以确保超过上限
    payload = _context("query-1\r\nX-Injected: bad" + ("x" * 2500))
    tokens = _set_dovie_context(payload)
    try:
        header_value = build_dovie_attribution_headers()["X-Dovie-Query-Id"]
    finally:
        clear_session_vars(tokens)

    assert "\r" not in header_value
    assert "\n" not in header_value
    assert len(header_value) == 2048
    assert header_value.startswith("query-1  X-Injected: bad")


def test_request_hook_does_not_overwrite_existing_header():
    tokens = _set_dovie_context(_context("query-from-context"))
    try:
        request = httpx.Request(
            "GET",
            "https://dovie.example.test/v1/chat/completions",
            headers={"X-Dovie-Query-Id": "caller-provided"},
        )
        dovie_attribution_request_hook(request)
    finally:
        clear_session_vars(tokens)

    assert request.headers["X-Dovie-Query-Id"] == "caller-provided"
    assert request.headers["X-Dovie-Root-Query-Id"] == "root-query-1"


@pytest.mark.asyncio
async def test_request_hook_reads_process_context_and_clear_removes_header():
    seen: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("X-Dovie-Query-Id", "")))
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(
        base_url="https://dovie.example.test",
        transport=httpx.MockTransport(handler),
    )
    attach_dovie_attribution_request_hook(client)

    async with client:
        tokens = _set_dovie_context(_context("query-a"))
        try:
            await client.get("/turn-a")
        finally:
            clear_session_vars(tokens)

        await client.get("/after-clear")

    assert seen == [
        ("/turn-a", "query-a"),
        ("/after-clear", ""),
    ]


@pytest.mark.asyncio
async def test_reused_client_reads_new_context_each_turn():
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["X-Dovie-Query-Id"])
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(
        base_url="https://dovie.example.test",
        transport=httpx.MockTransport(handler),
    )
    attach_dovie_attribution_request_hook(client)

    async with client:
        for query_id in ("query-a", "query-b"):
            tokens = _set_dovie_context(_context(query_id))
            try:
                await client.post("/v1/chat/completions")
            finally:
                clear_session_vars(tokens)

    assert seen == ["query-a", "query-b"]
