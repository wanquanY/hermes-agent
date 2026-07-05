from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

from agent.dovie_attribution import build_dovie_attribution_headers, dovie_child_run_overlay
from gateway.session_context import clear_session_vars, get_session_env, set_session_vars
from tools import async_delegation as ad
from tools.process_registry import process_registry


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HERMES_DOVIE_PRODUCT_CONTEXT", raising=False)
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    tokens = set_session_vars(dovie_product_context="")
    clear_session_vars(tokens)
    yield
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()
    tokens = set_session_vars(dovie_product_context="")
    clear_session_vars(tokens)


def _drain_one(timeout: float = 5.0) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_registry.completion_queue.empty():
            return process_registry.completion_queue.get_nowait()
        time.sleep(0.02)
    return None


def test_async_delegation_worker_reads_process_dovie_context_while_turn_active() -> None:
    seen: dict[str, str] = {}
    read = threading.Event()
    tokens = set_session_vars(dovie_product_context="sentinel-async-single")

    def runner() -> dict[str, Any]:
        seen["context"] = get_session_env("HERMES_DOVIE_PRODUCT_CONTEXT", "")
        assert seen["context"] == "sentinel-async-single"
        read.set()
        return {
            "status": "completed",
            "summary": "ok",
            "api_calls": 1,
            "duration_seconds": 0.1,
            "model": "m",
        }

    try:
        result = ad.dispatch_async_delegation(
            goal="g",
            context=None,
            toolsets=None,
            role="leaf",
            model="m",
            session_key="",
            runner=runner,
            max_async_children=1,
        )
        assert result["status"] == "dispatched"
        assert read.wait(timeout=2.0)
        event = _drain_one()
        assert event is not None
        assert event["summary"] == "ok"
        assert seen == {"context": "sentinel-async-single"}
    finally:
        clear_session_vars(tokens)


def test_async_delegation_batch_worker_reads_process_dovie_context_while_turn_active() -> None:
    seen: dict[str, str] = {}
    read = threading.Event()
    tokens = set_session_vars(dovie_product_context="sentinel-async-batch")

    def runner() -> dict[str, Any]:
        seen["context"] = get_session_env("HERMES_DOVIE_PRODUCT_CONTEXT", "")
        assert seen["context"] == "sentinel-async-batch"
        read.set()
        return {
            "results": [
                {
                    "task_index": 0,
                    "status": "completed",
                    "summary": "ok",
                    "api_calls": 1,
                    "duration_seconds": 0.1,
                    "model": "m",
                }
            ],
            "total_duration_seconds": 0.1,
        }

    try:
        result = ad.dispatch_async_delegation_batch(
            goals=["g"],
            context=None,
            toolsets=None,
            role="leaf",
            model="m",
            session_key="",
            runner=runner,
            max_async_children=1,
        )
        assert result["status"] == "dispatched"
        assert read.wait(timeout=2.0)
        event = _drain_one()
        assert event is not None
        assert event["is_batch"] is True
        assert event["results"][0]["summary"] == "ok"
        assert seen == {"context": "sentinel-async-batch"}
    finally:
        clear_session_vars(tokens)


def test_async_delegation_worker_receives_dovie_overlay_context() -> None:
    seen: dict[str, str] = {}
    read = threading.Event()
    tokens = set_session_vars(
        dovie_product_context=json.dumps(
            {
                "cloud_query": {
                    "query_id": "query-async",
                    "root_query_id": "root-query-async",
                    "agent_run_id": "root-run-async",
                    "query_context_token": "token-async",
                },
                "sourceAgentProfileId": "profile-root",
            }
        )
    )

    def runner() -> dict[str, Any]:
        headers = build_dovie_attribution_headers()
        seen["agent_run_id"] = headers["X-Dovie-Agent-Run-Id"]
        seen["executing_agent_profile_id"] = headers[
            "X-Dovie-Executing-Agent-Profile-Id"
        ]
        seen["agent_role"] = headers["X-Dovie-Agent-Role"]
        read.set()
        return {
            "status": "completed",
            "summary": "ok",
            "api_calls": 1,
            "duration_seconds": 0.1,
            "model": "m",
        }

    try:
        with dovie_child_run_overlay("profile-child", "subagent"):
            result = ad.dispatch_async_delegation(
                goal="g",
                context=None,
                toolsets=None,
                role="leaf",
                model="m",
                session_key="",
                runner=runner,
                max_async_children=1,
            )
        assert result["status"] == "dispatched"
        assert read.wait(timeout=2.0)
        event = _drain_one()
        assert event is not None
        assert event["summary"] == "ok"
    finally:
        clear_session_vars(tokens)

    assert seen == {
        "agent_run_id": "root-run-async",
        "executing_agent_profile_id": "profile-child",
        "agent_role": "subagent",
    }
