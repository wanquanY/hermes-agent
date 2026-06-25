"""Unit tests for ``tui_gateway.services.worker_runtime`` — Phase 5a.

Covers:
- ``is_primary_run_worker_mode`` env-flag matrix (primary / legacy /
  default / unknown one-time-warning)
- ``worker_supervisor`` / ``worker_frame_router`` singleton identity
- supervisor callbacks are bound to the router
- ``shutdown_run_worker_runtime`` is no-op when nothing spawned
- ``shutdown_run_worker_runtime`` clears singletons and calls
  ``supervisor.shutdown_all``
- ``_reset_for_tests`` clears singletons without touching subprocesses
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from tui_gateway.services import worker_runtime
from tui_gateway.services.worker_frame_router import WorkerFrameRouter
from tui_gateway.services.worker_supervisor import WorkerSupervisor


@pytest.fixture(autouse=True)
def _reset_singletons():
    worker_runtime._reset_for_tests()
    yield
    worker_runtime._reset_for_tests()


@pytest.mark.parametrize(
    "value, expected",
    [
        ("primary", True),
        ("PRIMARY", True),
        ("on", True),
        ("1", True),
        ("true", True),
        ("yes", True),
        ("", False),
        ("legacy", False),
        ("off", False),
        ("0", False),
        ("false", False),
        ("no", False),
    ],
)
def test_is_primary_run_worker_mode(monkeypatch, value, expected) -> None:
    monkeypatch.setenv("DOVIE_RUN_WORKER_MODE", value)
    assert worker_runtime.is_primary_run_worker_mode() is expected


def test_is_primary_default_unset(monkeypatch) -> None:
    monkeypatch.delenv("DOVIE_RUN_WORKER_MODE", raising=False)
    assert worker_runtime.is_primary_run_worker_mode() is False


def test_unknown_value_warns_once_and_treats_as_legacy(monkeypatch, caplog) -> None:
    monkeypatch.setenv("DOVIE_RUN_WORKER_MODE", "weird-value")
    with caplog.at_level(logging.WARNING, logger="tui_gateway.services.worker_runtime"):
        assert worker_runtime.is_primary_run_worker_mode() is False
        assert worker_runtime.is_primary_run_worker_mode() is False
    # Warning emitted exactly once.
    warnings = [r for r in caplog.records if "weird-value" in r.getMessage()]
    assert len(warnings) == 1


def test_singleton_identity() -> None:
    a = worker_runtime.worker_supervisor()
    b = worker_runtime.worker_supervisor()
    assert a is b
    assert isinstance(a, WorkerSupervisor)


def test_router_singleton_identity() -> None:
    a = worker_runtime.worker_frame_router()
    b = worker_runtime.worker_frame_router()
    assert a is b
    assert isinstance(a, WorkerFrameRouter)


def test_supervisor_callbacks_bound_to_router() -> None:
    sup = worker_runtime.worker_supervisor()
    router = worker_runtime.worker_frame_router()
    # The supervisor was constructed with router callbacks; verify by
    # poking at the private slots that the WorkerSupervisor stored
    # references to the same router methods.
    assert sup._on_event == router.on_event
    assert sup._on_interactive_request == router.on_interactive_request
    assert sup._on_run_terminal == router.on_run_terminal
    assert sup._on_log == router.on_log


@pytest.mark.asyncio
async def test_shutdown_noop_when_uninitialized() -> None:
    # Nothing constructed — must not raise.
    await worker_runtime.shutdown_run_worker_runtime()


@pytest.mark.asyncio
async def test_shutdown_clears_singletons_and_calls_shutdown_all() -> None:
    sup = worker_runtime.worker_supervisor()
    # Force the supervisor to record a shutdown_all call without actually
    # spawning anything by monkeypatching.
    sup.shutdown_all = AsyncMock()
    await worker_runtime.shutdown_run_worker_runtime()
    sup.shutdown_all.assert_awaited_once()
    # Singletons reset → next accessor returns a new instance.
    assert worker_runtime.worker_supervisor() is not sup


def test_reset_for_tests_drops_singletons() -> None:
    sup = worker_runtime.worker_supervisor()
    worker_runtime._reset_for_tests()
    assert worker_runtime.worker_supervisor() is not sup
