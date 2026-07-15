from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from hermes_agent.orchestration.worker_db_proxy import WorkerDBProxy
from hermes_agent.orchestration.worker_supervisor import WorkerSupervisor
from tui_gateway.run_worker import DBRpcRequestFrame


class _Writer:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def write_json(self, request: dict[str, Any]) -> None:
        with self._lock:
            self.requests.append(request)

    def wait_for_request(self) -> dict[str, Any]:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with self._lock:
                if self.requests:
                    return self.requests.pop(0)
            time.sleep(0.001)
        raise AssertionError("timed out waiting for worker DB request")


class _GraphQueries:
    def get_team_mission_graph(self, mission_id: str) -> dict[str, Any]:
        return {"mission": {"mission_id": mission_id}}


class _DB:
    def __init__(self) -> None:
        self.team_mission_graphs = _GraphQueries()


async def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def test_worker_proxy_emits_component_qualified_graph_query() -> None:
    writer = _Writer()
    proxy = WorkerDBProxy(writer, timeout_s=1)
    result: dict[str, Any] = {}
    thread = threading.Thread(
        target=lambda: result.update(
            proxy.team_mission_graphs.get_team_mission_graph("mission-1")
        )
    )

    thread.start()
    request = writer.wait_for_request()
    proxy.handle_reply(
        {
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"mission": {"mission_id": "mission-1"}},
        }
    )
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert request["method"] == "db.team_mission_graphs.get_team_mission_graph"
    assert request["params"] == [["mission-1"], {}]
    assert result == {"mission": {"mission_id": "mission-1"}}


@pytest.mark.asyncio
async def test_worker_supervisor_resolves_component_qualified_graph_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "hermes_agent.orchestration.worker_supervisor._db_for_worker_rpc",
        lambda _frame, _args, _kwargs: _DB(),
    )
    supervisor = WorkerSupervisor(
        on_event=_noop,
        on_interactive_request=_noop,
        on_run_terminal=_noop,
    )

    reply = await supervisor._execute_db_rpc(
        DBRpcRequestFrame(
            id="request-1",
            method="db.team_mission_graphs.get_team_mission_graph",
            params=[["mission-1"], {}],
            db_scope={},
        )
    )

    assert reply.error is None
    assert reply.result == {"mission": {"mission_id": "mission-1"}}
