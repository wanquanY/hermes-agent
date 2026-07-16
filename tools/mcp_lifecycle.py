"""Lifecycle primitives shared by every MCP transport."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable

DEFAULT_INITIALIZE_TIMEOUT = 30.0
MAX_INITIALIZE_TIMEOUT = 300.0


class MCPServerState(str, Enum):
    CREATED = "created"
    STARTING = "starting"
    CONNECTED = "connected"
    PARKED = "parked"
    STOPPING = "stopping"
    STOPPED = "stopped"


class MCPInitializeTimeout(TimeoutError):
    """Raised after an MCP initialize handshake exceeds its deadline."""


def normalize_initialize_timeout(value: Any, *, fallback: float) -> float:
    """Validate and cap an initialize timeout from untrusted config."""

    try:
        timeout = float(fallback if value is None else value)
    except (TypeError, ValueError):
        timeout = float(fallback)
    if not math.isfinite(timeout) or timeout <= 0:
        timeout = float(fallback)
    return min(max(timeout, 0.1), MAX_INITIALIZE_TIMEOUT)


async def cancel_and_drain(task: asyncio.Task | None) -> None:
    """Cancel a task and wait until its ``finally`` cleanup has completed."""

    if task is None or task.done():
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def bounded_initialize(
    session: Any,
    *,
    timeout: float,
    server_name: str,
    transport: str,
) -> Any:
    """Run ``session.initialize`` under an owned deadline.

    The child task is always drained before this function returns or raises,
    so caller cancellation cannot detach a handshake from its transport owner.
    """

    initialize_task = asyncio.create_task(
        session.initialize(),
        name=f"mcp-initialize:{server_name}:{transport}",
    )
    try:
        return await asyncio.wait_for(initialize_task, timeout=timeout)
    except asyncio.TimeoutError as exc:
        await cancel_and_drain(initialize_task)
        raise MCPInitializeTimeout(
            f"MCP server '{server_name}' {transport} initialize timed out after "
            f"{timeout:.1f}s"
        ) from exc
    except asyncio.CancelledError:
        await cancel_and_drain(initialize_task)
        raise


@dataclass(frozen=True)
class MCPParkPolicy:
    """Exponential self-probe policy for a parked MCP server."""

    initial_delay: float = 30.0
    maximum_delay: float = 300.0
    multiplier: float = 2.0

    def delay_for(self, consecutive_failures: int) -> float:
        failures = max(int(consecutive_failures), 1)
        initial = max(float(self.initial_delay), 0.1)
        maximum = max(float(self.maximum_delay), initial)
        multiplier = max(float(self.multiplier), 1.0)
        return min(initial * (multiplier ** (failures - 1)), maximum)


async def wait_for_wakeup_or_timeout(
    wakeup: asyncio.Event,
    shutdown: asyncio.Event,
    *,
    timeout: float,
) -> str:
    """Wait for manual wakeup, shutdown, or one parked-probe deadline."""

    wake_task = asyncio.create_task(wakeup.wait())
    stop_task = asyncio.create_task(shutdown.wait())
    try:
        done, _ = await asyncio.wait(
            {wake_task, stop_task},
            timeout=max(float(timeout), 0.0),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done and shutdown.is_set():
            return "shutdown"
        if wake_task in done and wakeup.is_set():
            wakeup.clear()
            return "wakeup"
        return "timeout"
    finally:
        await cancel_and_drain(wake_task)
        await cancel_and_drain(stop_task)
