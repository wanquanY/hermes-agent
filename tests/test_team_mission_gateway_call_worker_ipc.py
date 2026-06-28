from __future__ import annotations

from hermes_team_mission.runtime import profile_scope
from tui_gateway.services.worker_rpc_proxy import set_default_worker_rpc_proxy


class _Proxy:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, params=None):
        self.calls.append((method, params or {}))
        return self.response


def test_team_mission_gateway_call_routes_from_worker_to_control_plane_rpc():
    response = {"jsonrpc": "2.0", "id": "worker-team-mission:1", "result": {"mission_id": "m1"}}
    proxy = _Proxy(response)
    set_default_worker_rpc_proxy(proxy)
    try:
        result = profile_scope.gateway_call("team_mission.create", {"mission_id": "m1"})
    finally:
        set_default_worker_rpc_proxy(None)

    assert result == response
    assert proxy.calls == [
        (
            "worker.team_mission_gateway_call",
            {"method": "team_mission.create", "params": {"mission_id": "m1"}},
        )
    ]


def test_team_mission_gateway_call_rejects_unknown_methods_inside_worker():
    proxy = _Proxy({"jsonrpc": "2.0", "id": "unused", "result": {}})
    set_default_worker_rpc_proxy(proxy)
    try:
        result = profile_scope.gateway_call("session.resume", {"session_id": "s1"})
    finally:
        set_default_worker_rpc_proxy(None)

    assert result["error"]["message"] == (
        "Gateway method session.resume is not allowed from Team Mission worker runtime."
    )
    assert proxy.calls == []
