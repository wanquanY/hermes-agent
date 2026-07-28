from __future__ import annotations

from types import SimpleNamespace

import pytest

import agent.runtime_stability as runtime_stability
from agent.chat_completion_helpers import (
    interruptible_api_call,
    interruptible_streaming_api_call,
)
from agent.runtime_stability import (
    StreamStaleCircuitOpen,
    check_stream_stale_circuit,
    record_stream_stale_failure,
    record_stream_success,
    reset_stream_stale_circuit,
)
from hermes_agent.application.runtime_stability_service import (
    SessionRuntimeStabilityService,
)
from hermes_agent.composition.cli_session_store import open_cli_session_store
from hermes_agent.repositories.runtime_stability_repo import (
    RuntimeStabilityRepository,
)


def _agent(store, *, model: str = "model-a") -> SimpleNamespace:
    return SimpleNamespace(
        session_id="session-1",
        _session_db=store,
        provider="openai",
        base_url="https://example.invalid/v1",
        model=model,
        api_mode="chat_completions",
        _stream_stale_failures=0,
        _stream_stale_retry_after=0.0,
        _stream_stale_route_hash="",
        _interrupt_requested=False,
    )


def _install_clock(store, clock) -> None:
    store.runtime_stability = SessionRuntimeStabilityService(
        RuntimeStabilityRepository(store._conn),  # noqa: SLF001
        store._unit_of_work,  # noqa: SLF001
        clock=clock,
    )


def test_stream_stale_circuit_survives_agent_restart_and_explicit_reset(
    tmp_path,
    monkeypatch,
):
    now = [100.0]
    monkeypatch.setenv("HERMES_STREAM_STALE_GIVEUP", "2")
    monkeypatch.setenv("HERMES_STREAM_STALE_BREAKER_SECONDS", "30")
    monkeypatch.setattr(runtime_stability.time, "time", lambda: now[0])
    store = open_cli_session_store(tmp_path / "state.db")
    store.sessions.create("session-1", "cli")
    _install_clock(store, lambda: now[0])
    try:
        first_agent = _agent(store)
        assert record_stream_stale_failure(first_agent, "stale one") == 1
        assert record_stream_stale_failure(first_agent, "stale two") == 2

        resumed_agent = _agent(store)
        with pytest.raises(StreamStaleCircuitOpen, match="paused"):
            check_stream_stale_circuit(resumed_agent)

        reset_stream_stale_circuit(resumed_agent, reason="test_retry")
        check_stream_stale_circuit(_agent(store))
        assert store.runtime_stability.get("session-1").stream_stale_failures == 0
    finally:
        store.close()


def test_stream_stale_circuit_half_opens_after_policy_expiry(tmp_path, monkeypatch):
    now = [500.0]
    monkeypatch.setenv("HERMES_STREAM_STALE_GIVEUP", "2")
    monkeypatch.setenv("HERMES_STREAM_STALE_BREAKER_SECONDS", "10")
    monkeypatch.setattr(runtime_stability.time, "time", lambda: now[0])
    store = open_cli_session_store(tmp_path / "state.db")
    store.sessions.create("session-1", "cli")
    _install_clock(store, lambda: now[0])
    try:
        agent = _agent(store)
        record_stream_stale_failure(agent, "stale one")
        record_stream_stale_failure(agent, "stale two")
        with pytest.raises(StreamStaleCircuitOpen):
            check_stream_stale_circuit(_agent(store))

        now[0] = 511.0
        half_open_agent = _agent(store)
        check_stream_stale_circuit(half_open_agent)
        assert store.runtime_stability.get("session-1").stream_stale_failures == 0
    finally:
        store.close()


def test_route_change_and_success_clear_stream_stale_history(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STREAM_STALE_GIVEUP", "3")
    store = open_cli_session_store(tmp_path / "state.db")
    store.sessions.create("session-1", "cli")
    try:
        agent = _agent(store)
        record_stream_stale_failure(agent, "stale")

        changed_route = _agent(store, model="model-b")
        check_stream_stale_circuit(changed_route)
        assert store.runtime_stability.get("session-1").stream_stale_failures == 0

        record_stream_stale_failure(changed_route, "stale again")
        record_stream_success(changed_route)
        assert store.runtime_stability.get("session-1").stream_stale_failures == 0
    finally:
        store.close()


def test_streaming_and_non_streaming_paths_fail_fast_after_restart(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_STREAM_STALE_GIVEUP", "2")
    monkeypatch.setenv("HERMES_STREAM_STALE_BREAKER_SECONDS", "60")
    db_path = tmp_path / "state.db"
    first_store = open_cli_session_store(db_path)
    first_store.sessions.create("session-1", "cli")
    first_agent = _agent(first_store)
    record_stream_stale_failure(first_agent, "stale one")
    record_stream_stale_failure(first_agent, "stale two")
    first_store.close()

    resumed_store = open_cli_session_store(db_path)
    try:
        non_streaming_agent = _agent(resumed_store)
        with pytest.raises(StreamStaleCircuitOpen):
            interruptible_api_call(non_streaming_agent, {})

        streaming_agent = _agent(resumed_store)
        with pytest.raises(StreamStaleCircuitOpen):
            interruptible_streaming_api_call(streaming_agent, {})
    finally:
        resumed_store.close()


def test_worker_rpc_mapping_snapshot_restores_stream_breaker(monkeypatch):
    monkeypatch.setenv("HERMES_STREAM_STALE_GIVEUP", "2")
    expected_route = runtime_stability.stream_route_hash(_agent(None))
    store = SimpleNamespace(
        runtime_stability=SimpleNamespace(
            get=lambda _session_id: {
                "stream_stale_failures": 2,
                "stream_stale_retry_after": 10_000_000_000.0,
                "stream_stale_route_hash": expected_route,
            }
        )
    )

    with pytest.raises(StreamStaleCircuitOpen):
        check_stream_stale_circuit(_agent(store))
