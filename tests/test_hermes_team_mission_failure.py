from __future__ import annotations

from pathlib import Path

from hermes_team_mission.runtime.failure import classify_team_mission_failure
from hermes_state import SessionDB


def _bound_node_db(tmp_path: Path, *, output_contract: dict | None = None) -> SessionDB:
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-failure",
        team_id="team-1",
        title="Failure mission",
        objective="Exercise failure handling",
        mode="supervised_mission",
        status="running",
    )
    db.upsert_team_mission_node(
        mission_id="mission-failure",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        objective="Run worker",
        status="running",
        output_contract=output_contract or {},
    )
    db.upsert_run(run_id="run-worker", session_id="session-worker", status="running")
    db.bind_team_mission_run(
        mission_id="mission-failure",
        node_id="node-worker",
        run_id="run-worker",
        session_id="session-worker",
        runtime_scope_key="team:mission-failure:node:node-worker",
        role="worker",
    )
    return db


def test_team_mission_failure_classifies_truncated_tool_args():
    failure = classify_team_mission_failure(
        "message.complete",
        {
            "status": "error",
            "error": "Response truncated due to output length limit",
        },
    )

    assert failure["reason_code"] == "tool_args_truncated"
    assert failure["recoverability"] == "blocked"


def test_team_mission_failure_classifies_rate_limit_as_retryable():
    failure = classify_team_mission_failure(
        "error",
        {
            "error": "provider returned 429 rate limit quota exhausted",
        },
    )

    assert failure["reason_code"] == "provider_rate_limited"
    assert failure["recoverability"] == "retryable"


def test_team_mission_failure_prefers_provider_error_shape_retry_after():
    failure = classify_team_mission_failure(
        "error",
        {
            "error": {
                "kind": "rate_limit",
                "message": "requests per minute exceeded",
                "retry_after_s": 12,
            },
        },
    )

    assert failure["reason_code"] == "provider_rate_limited"
    assert failure["recoverability"] == "retryable"
    assert failure["retry_after_s"] == 12


def test_team_mission_failure_classifies_worker_crash():
    failure = classify_team_mission_failure(
        "error",
        {
            "error": "run_worker process crashed with exit code 137",
        },
    )

    assert failure["reason_code"] == "worker_crashed"
    assert failure["recoverability"] == "blocked"


def test_team_mission_terminal_error_projects_reason_code_to_events_and_node(tmp_path: Path):
    db = _bound_node_db(tmp_path)

    db.append_team_mission_run_event(
        mission_id="mission-failure",
        run_id="run-worker",
        event={
            "type": "message.complete",
            "seq": 1,
            "payload": {
                "status": "error",
                "error": "Response truncated due to output length limit",
            },
        },
    )

    node = db.get_team_mission_node("mission-failure", "node-worker")
    runtime_events = [
        event for event in db.list_team_mission_events("mission-failure")
        if event.get("payload", {}).get("source_event_type") == "message.complete"
    ]

    assert node["status"] == "failed"
    assert node["metadata"]["last_run_reason_code"] == "tool_args_truncated"
    assert runtime_events
    assert runtime_events[0]["payload"]["kind"] == "node.failed"
    assert runtime_events[0]["payload"]["reason_code"] == "tool_args_truncated"
    assert runtime_events[0]["payload"]["recoverability"] == "blocked"


def test_team_mission_clean_exit_without_required_handoff_blocks_as_protocol_violation(tmp_path: Path):
    db = _bound_node_db(
        tmp_path,
        output_contract={
            "format": "structured_deliverable",
            "delivery_channel": "handoff",
            "requires_explicit_handoff": True,
        },
    )

    db.append_team_mission_run_event(
        mission_id="mission-failure",
        run_id="run-worker",
        event={
            "type": "message.complete",
            "seq": 1,
            "payload": {
                "status": "complete",
                "text": "Visible answer without hidden handoff.",
            },
        },
    )

    node = db.get_team_mission_node("mission-failure", "node-worker")
    deliverable = db.latest_team_mission_deliverable_for_run("run-worker")
    blocked_events = [
        event for event in db.list_team_mission_events("mission-failure")
        if event.get("payload", {}).get("source_event_type") == "mission.node.blocked"
    ]

    assert node["status"] == "blocked"
    assert node["metadata"]["last_run_reason_code"] == "protocol_violation"
    assert deliverable["source"] == "missing"
    assert deliverable["status"] == "blocked"
    assert deliverable["payload"]["required_tool"] == "team_mission_submit_deliverable"
    assert db.team_mission_run_has_deliverable("run-worker") is False
    assert blocked_events
    assert blocked_events[0]["payload"]["kind"] == "node.blocked"
    assert blocked_events[0]["payload"]["reason_code"] == "protocol_violation"
    assert blocked_events[0]["payload"]["recoverability"] == "blocked"


def test_team_mission_requires_deliverable_implies_required_handoff(tmp_path: Path):
    db = _bound_node_db(
        tmp_path,
        output_contract={
            "format": "verification_report",
            "requires_deliverable": True,
        },
    )

    db.append_team_mission_run_event(
        mission_id="mission-failure",
        run_id="run-worker",
        event={
            "type": "message.complete",
            "seq": 1,
            "payload": {
                "status": "complete",
                "text": "Verifier report in visible text only.",
            },
        },
    )

    node = db.get_team_mission_node("mission-failure", "node-worker")
    deliverable = db.latest_team_mission_deliverable_for_run("run-worker")

    assert node["status"] == "blocked"
    assert node["metadata"]["last_run_reason_code"] == "protocol_violation"
    assert deliverable["source"] == "missing"
    assert deliverable["payload"]["required_tool"] == "team_mission_submit_deliverable"


def test_team_mission_missing_handoff_blocks_even_with_structured_final_text(tmp_path: Path):
    db = _bound_node_db(
        tmp_path,
        output_contract={
            "format": "structured_deliverable",
            "delivery_channel": "handoff",
            "requires_explicit_handoff": True,
        },
    )

    db.append_team_mission_run_event(
        mission_id="mission-failure",
        run_id="run-worker",
        event={
            "type": "message.complete",
            "seq": 1,
            "payload": {
                "status": "complete",
                "text": '{"node_id":"node-worker","status":"completed","summary":"structured but visible only"}',
            },
        },
    )

    node = db.get_team_mission_node("mission-failure", "node-worker")
    deliverable = db.latest_team_mission_deliverable_for_run("run-worker")
    blocked_events = [
        event for event in db.list_team_mission_events("mission-failure")
        if event.get("payload", {}).get("source_event_type") == "mission.node.blocked"
    ]

    assert node["status"] == "blocked"
    assert deliverable["source"] == "missing"
    assert deliverable["payload"]["required_tool"] == "team_mission_submit_deliverable"
    assert node["metadata"]["last_run_reason_code"] == "protocol_violation"
    assert blocked_events


def test_team_mission_submit_deliverable_terminal_completes_without_failure(tmp_path: Path):
    db = _bound_node_db(
        tmp_path,
        output_contract={
            "format": "structured_deliverable",
            "delivery_channel": "handoff",
            "requires_explicit_handoff": True,
        },
    )
    db.upsert_team_mission_deliverable(
        mission_id="mission-failure",
        node_id="node-worker",
        run_id="run-worker",
        status="completed",
        summary="Authoritative handoff submitted.",
        payload={"status": "completed"},
    )

    db.append_team_mission_run_event(
        mission_id="mission-failure",
        run_id="run-worker",
        event={
            "type": "message.complete",
            "seq": 1,
            "payload": {
                "status": "complete",
                "text": "Visible completion after hidden handoff.",
            },
        },
    )

    node = db.get_team_mission_node("mission-failure", "node-worker")

    assert node["status"] == "completed"
    assert node["metadata"].get("last_run_reason_code") in (None, "")
