from __future__ import annotations

from doxie_extension import DoxieHermesExtension, load_extension
from doxie_extension.gateway_methods import doxie_gateway_method_overrides
from doxie_extension.manifest import gateway_capabilities as extension_gateway_capabilities
from tui_gateway.doxie_gateway_contract import (
    REQUIRED_METHODS,
    gateway_capabilities,
)


def test_doxie_gateway_capabilities_reports_complete_gateway_abi():
    manifest = gateway_capabilities()

    assert manifest["ok"] is True
    assert manifest["protocolVersion"] == "2026-05-23"
    for method in REQUIRED_METHODS:
        assert method in manifest["methods"]
    assert "message.delta" in manifest["events"]
    assert "runtime:doxie_sidecar" in manifest["runtimeFeatures"]
    assert "state:message_reasoning" in manifest["stateFeatures"]
    assert "state:session_search" in manifest["stateFeatures"]
    assert "state:run_registry" in manifest["stateFeatures"]
    assert "state:run_event_log" in manifest["stateFeatures"]
    assert "state:runtime_scope_key" in manifest["stateFeatures"]
    assert "state:transient_session" in manifest["stateFeatures"]
    assert manifest["missingCapabilities"] == []


def test_doxie_gateway_contract_is_served_by_extension_manifest():
    assert gateway_capabilities is extension_gateway_capabilities
    extension = load_extension()
    assert isinstance(extension, DoxieHermesExtension)
    assert extension.register_capabilities()["extensionVersion"] == extension.version
    assert "run.events" in extension.gateway_method_overrides()
    assert extension.gateway_method_overrides() == doxie_gateway_method_overrides()


def test_gateway_capabilities_json_rpc_method_is_registered():
    import importlib

    from tui_gateway import server

    importlib.import_module("tui_gateway.methods.run")
    response = server._methods["gateway.capabilities"](1, {})

    assert response["result"]["protocolVersion"] == "2026-05-23"
    assert "run.submit" in response["result"]["methods"]
    assert "run.events" in response["result"]["methods"]
    assert "events.unsubscribe" in response["result"]["methods"]
    assert "session.usage" in response["result"]["methods"]
    assert "session.status" in response["result"]["methods"]
    assert "prompt.submit" in response["result"]["methods"]
    assert "model.set" in response["result"]["methods"]
    assert "model.options" in response["result"]["methods"]
    assert "toolsets.list" in response["result"]["methods"]
    assert "profile.prepare_runtime" in response["result"]["methods"]
    assert "runtime.status" in response["result"]["methods"]
    assert "approval.respond" in response["result"]["methods"]
    assert "sudo.respond" in response["result"]["methods"]
    assert "secret.respond" in response["result"]["methods"]
    assert "clarify.respond" in response["result"]["methods"]
    assert "run.events" in server._methods
    assert "events.unsubscribe" in server._methods
    assert "session.usage" in server._methods
    assert "session.status" in server._methods
    assert server._methods["session.status"].__module__ == "tui_gateway.methods.session"
    assert server._methods["prompt.submit"].__module__ == "tui_gateway.methods.prompt"
    assert "model.set" in server._methods
    assert "model.options" in server._methods
    assert "toolsets.list" in server._methods
    assert "profile.prepare_runtime" in server._methods
    assert "runtime.status" in server._methods
    assert "approval.respond" in server._methods
    assert "sudo.respond" in server._methods
    assert "secret.respond" in server._methods
    assert "clarify.respond" in server._methods


def test_extracted_gateway_methods_own_registered_handlers():
    from tui_gateway import server

    assert server._methods["prompt.submit"].__module__ == "tui_gateway.methods.prompt"
    assert server._methods["model.set"].__module__ == "tui_gateway.methods.model"
    assert server._methods["model.options"].__module__ == "tui_gateway.methods.model"
    assert server._methods["run.events"].__module__ == "tui_gateway.methods.run"
    assert server._methods["events.unsubscribe"].__module__ == "tui_gateway.methods.run"
    assert server._methods["session.resume"].__module__ == "tui_gateway.methods.session"
    assert server._methods["config.show"].__module__ == "tui_gateway.methods.integrations"
    assert server._methods["skills.reload"].__module__ == "tui_gateway.methods.integrations"
    assert {
        getattr(handler, "__module__", "")
        for name, handler in server._methods.items()
        if not name.startswith("_")
    }.isdisjoint({"tui_gateway.server"})


def test_model_set_gateway_method_uses_stable_params(monkeypatch):
    from types import SimpleNamespace

    from tui_gateway import server

    agent = SimpleNamespace()
    session = {"running": False, "agent": agent}
    monkeypatch.setitem(server._sessions, "runtime-1", session)
    calls = []

    def fake_apply_model_switch(session_id, active_session, model):
        calls.append((session_id, active_session, model))
        return {"value": model, "warning": ""}

    monkeypatch.setattr(server, "_apply_model_switch", fake_apply_model_switch)
    response = server._methods["model.set"](
        1,
        {
            "model": "gpt-5",
            "session_id": "runtime-1",
            "model_descriptor": {"id": "gpt-5", "provider": "openai"},
        },
    )

    assert response["result"] == {"key": "model", "value": "gpt-5", "warning": ""}
    assert calls == [("runtime-1", session, "gpt-5")]
    assert session["model_descriptor"] == {"id": "gpt-5", "provider": "openai"}
    assert agent.model_descriptor == {"id": "gpt-5", "provider": "openai"}


def test_model_set_gateway_method_rejects_running_session(monkeypatch):
    from tui_gateway import server

    monkeypatch.setitem(
        server._sessions,
        "runtime-busy",
        {"running": True, "agent": object()},
    )

    def fail_apply_model_switch(*_args, **_kwargs):
        raise AssertionError("model switch must not run while session is busy")

    monkeypatch.setattr(server, "_apply_model_switch", fail_apply_model_switch)
    response = server._methods["model.set"](
        1,
        {
            "model": "gpt-5",
            "session_id": "runtime-busy",
        },
    )

    assert response["error"]["code"] == 4009


def test_profile_prepare_runtime_returns_canonical_runtime_scope():
    from tui_gateway import server

    response = server._methods["profile.prepare_runtime"](
        1,
        {
            "agentProfileId": "agent-a",
            "agentProfileVersionId": "version-1",
        },
    )
    assert response["result"]["prepared"] is True
    assert response["result"]["status"] == "prepared"
    assert response["result"]["agent_profile_id"] == "agent-a"
    assert response["result"]["agent_profile_version_id"] == "version-1"
    assert response["result"]["runtime_scope_key"] == "profile:agent-a:version:version-1"
    assert response["result"]["transient"] is False


def test_profile_prepare_runtime_preserves_draft_scope():
    from tui_gateway import server

    response = server._methods["profile.prepare_runtime"](
        1,
        {
            "agentProfileDraftId": "draft-1",
            "runtimeScopeKey": "draft:draft-1",
        },
    )
    assert response["result"]["prepared"] is True
    assert response["result"]["agent_profile_draft_id"] == "draft-1"
    assert response["result"]["runtime_scope_key"] == "draft:draft-1"
    assert response["result"]["transient"] is True


def test_runtime_status_returns_lightweight_diagnostics(monkeypatch):
    from tui_gateway import server

    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {"gateway_state": "running", "pid": 1234},
    )
    response = server._methods["runtime.status"](1, {})

    assert response["result"]["status"] == "running"
    assert response["result"]["available"] is True
    assert response["result"]["runtime"]["pid"] == 1234


def test_session_db_persists_run_registry_and_event_log(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("session-1", "tui", transient=True)

        reservation = db.create_run_if_session_idle(
            run_id="run-1",
            session_id="session-1",
            runtime_scope_key="profile:agent-default",
            turn_id="turn-1",
            runtime_session_id="runtime-1",
            status="queued",
            metadata={"source": "test"},
        )
        assert reservation["created"] is True
        assert reservation["conflict"] is None
        assert reservation["run"]["runtime_scope_key"] == "profile:agent-default"

        conflict = db.create_run_if_session_idle(
            run_id="run-2",
            session_id="session-1",
            runtime_scope_key="profile:agent-default",
            turn_id="turn-2",
            runtime_session_id="runtime-1",
            status="queued",
        )
        assert conflict["created"] is False
        assert conflict["conflict"]["run_id"] == "run-1"

        event = db.append_run_event(
            "session-1",
            {
                "type": "message.delta",
                "session_id": "runtime-1",
                "stored_session_id": "session-1",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "runtime_scope_key": "profile:agent-default",
                "seq": 1,
                "payload": {"text": "hello"},
            },
        )
        assert event["seq"] == 1

        events = db.list_run_events(
            "session-1",
            runtime_scope_key="profile:agent-default",
        )
        assert [item["seq"] for item in events] == [1]

        run = db.get_run("run-1")
        assert run["last_seq"] == 1
        assert run["metadata"]["source"] == "test"

        rows = db._conn.execute("SELECT transient FROM sessions WHERE id = ?", ("session-1",)).fetchone()
        assert rows["transient"] == 1
    finally:
        db.close()


def test_session_db_keeps_terminal_run_closed_after_late_delta(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("session-1", "tui")
        first = db.create_run_if_session_idle(
            run_id="run-1",
            session_id="session-1",
            runtime_scope_key="profile:agent-default",
            turn_id="turn-1",
            runtime_session_id="runtime-1",
            status="running",
        )
        assert first["created"] is True

        db.append_run_event(
            "session-1",
            {
                "type": "message.complete",
                "session_id": "runtime-1",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "runtime_scope_key": "profile:agent-default",
                "seq": 2,
                "payload": {"status": "complete"},
            },
        )
        assert db.get_run("run-1")["status"] == "completed"

        db.append_run_event(
            "session-1",
            {
                "type": "message.delta",
                "session_id": "runtime-1",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "runtime_scope_key": "profile:agent-default",
                "seq": 3,
                "payload": {"text": "late"},
            },
        )

        terminal = db.get_run("run-1")
        assert terminal["status"] == "completed"
        assert terminal["last_seq"] == 3
        assert terminal["completed_at"] is not None
        assert db.list_run_events(
            "session-1",
            active_only=True,
            runtime_scope_key="profile:agent-default",
        ) == []

        second = db.create_run_if_session_idle(
            run_id="run-2",
            session_id="session-1",
            runtime_scope_key="profile:agent-default",
            turn_id="turn-2",
            runtime_session_id="runtime-2",
            status="queued",
        )
        assert second["created"] is True
        assert second["conflict"] is None
    finally:
        db.close()


def test_session_db_replays_run_events_by_runtime_scope(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("session-1", "tui")
        for run_id, scope, seq, text in (
            ("run-v1", "profile:agent-a:version:v1", 1, "v1"),
            ("run-v2", "profile:agent-a:version:v2", 2, "v2"),
            ("run-default", "profile:agent-default", 3, "default"),
        ):
            db.append_run_event(
                "session-1",
                {
                    "type": "message.delta",
                    "session_id": f"runtime-{run_id}",
                    "run_id": run_id,
                    "turn_id": f"turn-{run_id}",
                    "runtime_scope_key": scope,
                    "seq": seq,
                    "payload": {"text": text},
                },
            )

        v1_events = db.list_run_events(
            "session-1",
            runtime_scope_key="profile:agent-a:version:v1",
        )
        assert [item["run_id"] for item in v1_events] == ["run-v1"]
        assert [item["payload"]["text"] for item in v1_events] == ["v1"]

        v2_events = db.list_run_events(
            "session-1",
            runtime_scope_key="profile:agent-a:version:v2",
        )
        assert [item["run_id"] for item in v2_events] == ["run-v2"]

        assert db.list_run_events(
            "session-1",
            after_seq=2,
            runtime_scope_key="profile:agent-a:version:v1",
        ) == []
        assert [
            item["run_id"]
            for item in db.list_run_events("session-1", after_seq=2)
        ] == ["run-default"]
    finally:
        db.close()
