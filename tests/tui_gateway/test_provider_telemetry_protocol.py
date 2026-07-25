from tui_gateway.services.runtime_event_protocol import (
    DIAGNOSTICS_EVENT_DOMAIN,
    event_domain_for_type,
    is_transient_platform_event,
    mark_transient,
)
from hermes_agent.composition.cli_session_store import open_cli_session_store
from tui_gateway.services import run_control


def test_provider_telemetry_uses_diagnostics_side_channel():
    frame = {
        "type": "runtime.provider.telemetry",
        "seq": 42,
        "payload": {"seq": 42},
    }

    assert is_transient_platform_event(frame) is True
    assert event_domain_for_type(frame["type"]) == DIAGNOSTICS_EVENT_DOMAIN

    mark_transient(frame)

    assert frame["transient"] is True
    assert frame["event_domain"] == "diagnostics"
    assert "seq" not in frame
    assert "seq" not in frame["payload"]


def test_provider_telemetry_never_enters_durable_or_memory_replay(tmp_path):
    run_control._reset_for_tests()
    db = open_cli_session_store(tmp_path / "provider-telemetry.db")
    frame = {
        "type": "runtime.provider.telemetry",
        "conversation_session_id": "conversation-1",
        "session_id": "execution-1",
        "run_id": "run-1",
        "turn_id": "turn-1",
        "runtime_scope_key": "profile:agent-1",
        "seq": 27,
        "payload": {
            "schema": "hermes.provider-telemetry.v1",
            "stage": "provider.call.started",
        },
    }

    run_control.record_event(frame, db=db)
    _subscription_id, replay = run_control.subscribe_session_with_id(
        conversation_session_id="conversation-1",
        transport=None,
        db=db,
    )

    assert frame["transient"] is True
    assert frame["event_domain"] == "diagnostics"
    assert "seq" not in frame
    assert db.runs.list_events("conversation-1") == []
    assert replay == []
    db.close()
    run_control._reset_for_tests()
