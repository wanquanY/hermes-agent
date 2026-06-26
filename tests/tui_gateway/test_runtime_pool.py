from tui_gateway.services.runtime_pool import (
    RuntimeLease,
    RuntimeLeaseError,
    acquire_runtime_lease,
)


def test_acquire_runtime_lease_reuses_live_runtime():
    session = {"session_key": "stored-1", "transport": None, "agent": object()}
    calls = {"resume": 0}

    lease = acquire_runtime_lease(
        rid="r1",
        stored_session_id="stored-1",
        params={},
        resolve_runtime_session=lambda _target: ("runtime-1", session),
        resume_runtime_session=lambda _rid, _params: calls.__setitem__("resume", 1),
        session_lookup=lambda _sid: None,
        transport="transport",
        fallback_transport="fallback",
    )

    assert isinstance(lease, RuntimeLease)
    assert lease.runtime_session_id == "runtime-1"
    assert lease.session is session
    assert lease.reused is True
    assert session["transport"] == "transport"
    assert calls["resume"] == 0


def test_acquire_runtime_lease_rebuilds_missing_runtime_without_hydration():
    sessions = {"runtime-2": {"session_key": "stored-2", "agent_ready": object()}}
    captured = {}

    def resume(_rid, params):
        captured.update(params)
        return {"result": {"session_id": "runtime-2"}}

    lease = acquire_runtime_lease(
        rid="r1",
        stored_session_id="stored-2",
        params={"text": "hello"},
        resolve_runtime_session=lambda _target: ("", None),
        resume_runtime_session=resume,
        session_lookup=lambda sid: sessions.get(sid),
        transport=None,
        fallback_transport="fallback",
    )

    assert isinstance(lease, RuntimeLease)
    assert lease.runtime_session_id == "runtime-2"
    assert lease.reused is False
    assert captured["session_id"] == "stored-2"
    assert captured["hydrate"] == "none"
    assert captured["message_limit"] == 0
    assert captured["_runtime_attach"] is True
    assert sessions["runtime-2"]["transport"] == "fallback"


def test_acquire_runtime_lease_rebuilds_scope_mismatch():
    live = {"session_key": "stored-3", "runtime_scope_key": "profile:old", "agent": object()}
    sessions = {"runtime-3b": {"session_key": "stored-3", "agent_ready": object()}}
    captured = {}

    def resume(_rid, params):
        captured.update(params)
        return {"result": {"session_id": "runtime-3b"}}

    lease = acquire_runtime_lease(
        rid="r1",
        stored_session_id="stored-3",
        params={},
        resolve_runtime_session=lambda _target: ("runtime-3a", live),
        resume_runtime_session=resume,
        session_lookup=lambda sid: sessions.get(sid),
        transport=None,
        fallback_transport="fallback",
        runtime_scope_key="profile:new",
    )

    assert isinstance(lease, RuntimeLease)
    assert lease.runtime_session_id == "runtime-3b"
    assert lease.reused is False
    assert captured["runtime_scope_key"] == "profile:new"
    assert sessions["runtime-3b"]["runtime_scope_key"] == "profile:new"


def test_acquire_runtime_lease_rebuilds_context_mode_mismatch():
    live = {
        "session_key": "stored-team",
        "runtime_scope_key": "team:stored-team:leader-conversation",
        "agent_context_mode": "profile",
        "agent": object(),
    }
    sessions = {
        "runtime-team": {
            "session_key": "stored-team",
            "runtime_scope_key": "team:stored-team:leader-conversation",
            "agent_context_mode": "team_leader",
            "agent_ready": object(),
        }
    }
    captured = {}

    def resume(_rid, params):
        captured.update(params)
        return {"result": {"session_id": "runtime-team"}}

    lease = acquire_runtime_lease(
        rid="r1",
        stored_session_id="stored-team",
        params={"agent_context_mode": "team_leader"},
        resolve_runtime_session=lambda _target: ("runtime-profile", live),
        resume_runtime_session=resume,
        session_lookup=lambda sid: sessions.get(sid),
        transport=None,
        fallback_transport="fallback",
        runtime_scope_key="team:stored-team:leader-conversation",
    )

    assert isinstance(lease, RuntimeLease)
    assert lease.runtime_session_id == "runtime-team"
    assert lease.reused is False
    assert captured["agent_context_mode"] == "team_leader"


def test_acquire_runtime_lease_returns_resume_error():
    lease = acquire_runtime_lease(
        rid="r1",
        stored_session_id="missing",
        params={},
        resolve_runtime_session=lambda _target: ("", None),
        resume_runtime_session=lambda _rid, _params: {
            "error": {"code": 4007, "message": "session not found"}
        },
        session_lookup=lambda _sid: None,
        transport=None,
        fallback_transport=None,
    )

    assert isinstance(lease, RuntimeLeaseError)
    assert lease.response["error"]["code"] == 4007


def test_acquire_runtime_lease_rejects_control_plane_only_session():
    control_plane = {"session_key": "stored-4", "control_plane_only": True}

    lease = acquire_runtime_lease(
        rid="r1",
        stored_session_id="stored-4",
        params={},
        resolve_runtime_session=lambda _target: ("runtime-4", control_plane),
        resume_runtime_session=lambda _rid, _params: {"result": {"session_id": "runtime-4b"}},
        session_lookup=lambda _sid: {"session_key": "stored-4", "control_plane_only": True},
        transport=None,
        fallback_transport=None,
    )

    assert isinstance(lease, RuntimeLeaseError)
    assert lease.response["error"]["message"] == "runtime session resumed but is not executable"
