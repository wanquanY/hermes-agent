from __future__ import annotations

import time
from typing import Any

import pytest

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


def test_async_delegation_worker_preserves_dispatch_dovie_context() -> None:
    seen: dict[str, str] = {}
    tokens = set_session_vars(dovie_product_context="sentinel-async-single")

    def runner() -> dict[str, Any]:
        seen["context"] = get_session_env("HERMES_DOVIE_PRODUCT_CONTEXT", "")
        assert seen["context"] == "sentinel-async-single"
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
    finally:
        clear_session_vars(tokens)

    assert result["status"] == "dispatched"
    event = _drain_one()
    assert event is not None
    assert event["summary"] == "ok"
    assert seen == {"context": "sentinel-async-single"}


def test_async_delegation_batch_worker_preserves_dispatch_dovie_context() -> None:
    seen: dict[str, str] = {}
    tokens = set_session_vars(dovie_product_context="sentinel-async-batch")

    def runner() -> dict[str, Any]:
        seen["context"] = get_session_env("HERMES_DOVIE_PRODUCT_CONTEXT", "")
        assert seen["context"] == "sentinel-async-batch"
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
    finally:
        clear_session_vars(tokens)

    assert result["status"] == "dispatched"
    event = _drain_one()
    assert event is not None
    assert event["is_batch"] is True
    assert event["results"][0]["summary"] == "ok"
    assert seen == {"context": "sentinel-async-batch"}
