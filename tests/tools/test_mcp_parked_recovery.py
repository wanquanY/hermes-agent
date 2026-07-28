import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.mcp_lifecycle import MCPServerState


def _tool(name: str):
    return SimpleNamespace(
        name=name,
        description=name,
        inputSchema={"type": "object", "properties": {}},
    )


@pytest.mark.asyncio
async def test_parked_owner_self_probes_and_atomically_republishes_once():
    from tools.mcp_tool import (
        MCPServerTask,
        _discover_and_register_server,
        _servers,
    )
    from tools.registry import ToolRegistry

    registry = ToolRegistry()
    attempts = 0
    recovered = asyncio.Event()
    allow_probe = asyncio.Event()
    session = MagicMock()

    async def fake_stdio(owner, _config):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("cold start unavailable")
        owner.session = session
        owner._tools = [_tool("ping")]
        owner._mark_connected()
        recovered.set()
        await owner._shutdown_event.wait()

    async def immediate_park_deadline(_wakeup, shutdown, *, timeout):
        if shutdown.is_set():
            return "shutdown"
        await allow_probe.wait()
        return "timeout"

    try:
        with (
            patch("tools.registry.registry", registry),
            patch.object(MCPServerTask, "_run_stdio", new=fake_stdio),
            patch("tools.mcp_tool._MAX_INITIAL_CONNECT_RETRIES", 0),
            patch(
                "tools.mcp_lifecycle.wait_for_wakeup_or_timeout",
                side_effect=immediate_park_deadline,
            ),
        ):
            initially_registered = await _discover_and_register_server(
                "revive",
                {
                    "command": "fake",
                    "parked_retry_interval": 0.1,
                    "parked_retry_max_interval": 0.2,
                },
            )
            owner = _servers["revive"]
            assert initially_registered == []
            assert owner.state == MCPServerState.PARKED
            assert registry.get_all_tool_names() == []
            allow_probe.set()
            await asyncio.wait_for(recovered.wait(), timeout=1)

            assert owner.state == MCPServerState.CONNECTED
            assert owner.last_error is None
            assert owner.next_probe_at is None
            assert attempts == 2
            assert registry.get_all_tool_names() == [
                "mcp__revive__get_prompt",
                "mcp__revive__list_prompts",
                "mcp__revive__list_resources",
                "mcp__revive__ping",
                "mcp__revive__read_resource",
            ]

            # Discovery reads the retained owner and cannot launch a second
            # task or publish duplicates.
            again = await _discover_and_register_server(
                "revive", {"command": "different"}
            )
            assert set(again) == set(registry.get_all_tool_names())
            assert attempts == 2

            await owner.shutdown()
            assert owner.state == MCPServerState.STOPPED
            assert registry.get_all_tool_names() == []
    finally:
        owner = _servers.pop("revive", None)
        if owner is not None and owner._task is not None and not owner._task.done():
            await owner.shutdown()


@pytest.mark.asyncio
async def test_hung_stdio_initialize_parks_after_transport_cleanup():
    from tools.mcp_tool import MCPServerTask

    gate = asyncio.Event()
    session = MagicMock()
    initialize_cancelled = asyncio.Event()

    async def hung_initialize():
        try:
            await gate.wait()
        except asyncio.CancelledError:
            initialize_cancelled.set()
            raise

    session.initialize = hung_initialize
    session.list_tools = AsyncMock()

    stdio_cm = MagicMock()
    stdio_cm.__aenter__ = AsyncMock(
        return_value=(MagicMock(), MagicMock())
    )
    stdio_cm.__aexit__ = AsyncMock(return_value=False)
    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=session)
    client_cm.__aexit__ = AsyncMock(return_value=False)

    owner = MCPServerTask("hung", publish_tools=False)
    with (
        patch("tools.mcp_tool.StdioServerParameters"),
        patch("tools.mcp_tool.stdio_client", return_value=stdio_cm),
        patch("tools.mcp_tool.ClientSession", return_value=client_cm),
        patch("tools.mcp_tool._MAX_INITIAL_CONNECT_RETRIES", 0),
    ):
        await owner.start(
            {
                "command": "fake",
                "initialize_timeout": 0.01,
                "parked_retry_interval": 60,
            }
        )

    assert owner.state == MCPServerState.PARKED
    assert initialize_cancelled.is_set()
    assert client_cm.__aexit__.await_count == 1
    assert stdio_cm.__aexit__.await_count == 1
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and task.get_name().startswith("mcp-initialize:hung")
    ]

    await owner.shutdown()
    assert owner._task is not None and owner._task.done()


@pytest.mark.asyncio
async def test_start_cancellation_drains_owned_transport_task():
    from tools.mcp_tool import MCPServerTask

    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_run(owner, _config):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            owner.state = MCPServerState.STOPPED
            raise

    owner = MCPServerTask("cancel", publish_tools=False)
    with patch.object(MCPServerTask, "run", new=blocked_run):
        start_task = asyncio.create_task(owner.start({"command": "fake"}))
        await entered.wait()
        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task

    assert cancelled.is_set()
    assert owner._task is not None and owner._task.done()
