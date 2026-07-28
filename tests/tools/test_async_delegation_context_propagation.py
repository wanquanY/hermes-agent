from __future__ import annotations

import json
import threading
import time

from agent.dovie_attribution import build_dovie_attribution_headers, dovie_child_run_overlay
from channels.session_context import clear_session_vars, get_session_env, set_session_vars
from hermes_agent.application.subagent_execution_service import (
    ExecutionMode,
    SubagentExecutionRuntime,
    SubagentExecutionService,
    SubagentTaskSpec,
)
from hermes_agent.composition.cli_session_store import open_cli_session_store


def _runtime_plan(tmp_path):
    service = SubagentExecutionService(
        state_store=open_cli_session_store(tmp_path / "state.db"),
        conversation_session_id="conversation",
    )
    plan = service.create_plan(
        [
            SubagentTaskSpec(
                task_index=0,
                goal="goal",
                child_session_id="child",
            )
        ],
        mode=ExecutionMode.ASYNC,
    )
    return SubagentExecutionRuntime(), service, plan


def _wait(runtime: SubagentExecutionRuntime) -> None:
    deadline = time.monotonic() + 3
    while runtime.active_count() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert runtime.active_count() == 0


def test_async_runtime_carries_session_contextvars(tmp_path) -> None:
    runtime, service, plan = _runtime_plan(tmp_path)
    seen = {}
    tokens = set_session_vars(dovie_product_context="sentinel-async")
    try:
        runtime.submit(
            plan=plan,
            service=service,
            runner=lambda: (
                seen.update(
                    context=get_session_env("HERMES_DOVIE_PRODUCT_CONTEXT", "")
                ),
                {
                    "results": [
                        {"task_index": 0, "status": "completed", "summary": "ok"}
                    ]
                },
            )[1],
            interrupt_fn=None,
            max_workers=1,
        )
        _wait(runtime)
    finally:
        clear_session_vars(tokens)
    assert seen == {"context": "sentinel-async"}


def test_async_runtime_carries_dovie_child_overlay(tmp_path) -> None:
    runtime, service, plan = _runtime_plan(tmp_path)
    seen = {}
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

    def runner():
        headers = build_dovie_attribution_headers()
        seen.update(
            agent_run_id=headers["X-Dovie-Agent-Run-Id"],
            executing_agent_profile_id=headers[
                "X-Dovie-Executing-Agent-Profile-Id"
            ],
            agent_role=headers["X-Dovie-Agent-Role"],
        )
        return {
            "results": [
                {"task_index": 0, "status": "completed", "summary": "ok"}
            ]
        }

    try:
        with dovie_child_run_overlay("profile-child", "subagent"):
            runtime.submit(
                plan=plan,
                service=service,
                runner=runner,
                interrupt_fn=None,
                max_workers=1,
            )
        _wait(runtime)
    finally:
        clear_session_vars(tokens)
    assert seen == {
        "agent_run_id": "root-run-async",
        "executing_agent_profile_id": "profile-child",
        "agent_role": "subagent",
    }


def test_async_runtime_context_isolated_between_dispatches(tmp_path) -> None:
    runtime_one, service_one, plan_one = _runtime_plan(tmp_path / "one")
    runtime_two, service_two, plan_two = _runtime_plan(tmp_path / "two")
    seen: list[str] = []
    lock = threading.Lock()

    def dispatch(runtime, service, plan, value):
        def runner():
            with lock:
                seen.append(get_session_env("HERMES_SESSION_KEY", ""))
            return {
                "results": [
                    {
                        "task_index": 0,
                        "status": "completed",
                        "summary": value,
                    }
                ]
            }

        tokens = set_session_vars(session_key=value)
        try:
            runtime.submit(
                plan=plan,
                service=service,
                runner=runner,
                interrupt_fn=None,
                max_workers=1,
            )
        finally:
            clear_session_vars(tokens)

    dispatch(runtime_one, service_one, plan_one, "one")
    dispatch(runtime_two, service_two, plan_two, "two")
    _wait(runtime_one)
    _wait(runtime_two)
    assert sorted(seen) == ["one", "two"]
