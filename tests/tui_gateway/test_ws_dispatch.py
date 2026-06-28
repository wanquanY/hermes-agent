import asyncio
import json
import os
import sys
import types
import time

import pytest

from tui_gateway import ws
from tui_gateway.methods.prompt import _prompt_terminal_status_from_result
from tui_gateway.services import runtime_proxy


def test_prompt_terminal_status_keeps_usable_response_complete_with_nonfatal_error():
    result = {
        "final_response": "验证结果：不通过\n\n原因：时间格式不符合要求。",
        "error": "subagent returned failed status",
        "failed": True,
    }

    assert _prompt_terminal_status_from_result(result, result["final_response"]) == "complete"


def test_prompt_terminal_status_reports_error_when_failed_without_usable_response():
    assert _prompt_terminal_status_from_result(
        {"error": "provider failed", "failed": True},
        "",
    ) == "error"
    assert _prompt_terminal_status_from_result(
        {"error": "provider failed", "failed": True},
        "Error: provider failed",
    ) == "error"


def test_workspace_current_uses_control_plane_executor():
    executor = ws._executor_for_request(  # noqa: SLF001
        {"id": "1", "method": "workspace.current", "params": {"session_id": "s"}}
    )

    assert executor is ws._ws_control_executor  # noqa: SLF001


def test_session_list_uses_control_plane_executor():
    executor = ws._executor_for_request(  # noqa: SLF001
        {"id": "1", "method": "session.list", "params": {}}
    )

    assert executor is ws._ws_control_executor  # noqa: SLF001


def test_session_title_uses_control_plane_executor():
    executor = ws._executor_for_request(  # noqa: SLF001
        {"id": "1", "method": "session.title", "params": {"stored_session_id": "s"}}
    )

    assert executor is ws._ws_control_executor  # noqa: SLF001


def test_run_status_uses_control_plane_executor():
    executor = ws._executor_for_request(  # noqa: SLF001
        {"id": "1", "method": "run.status", "params": {"stored_session_id": "s"}}
    )

    assert executor is ws._ws_control_executor  # noqa: SLF001


def test_team_mission_message_submit_uses_control_plane_executor():
    executor = ws._executor_for_request(  # noqa: SLF001
        {
            "id": "1",
            "method": "team_mission.message.submit",
            "params": {
                "conversation_id": "conversation-1",
                "conversation_session_id": "team-session-1",
            },
        }
    )

    assert executor is ws._ws_control_executor  # noqa: SLF001


def test_events_unsubscribe_uses_control_plane_executor():
    executor = ws._executor_for_request(  # noqa: SLF001
        {"id": "1", "method": "events.unsubscribe", "params": {"subscription_id": "sub"}}
    )

    assert executor is ws._ws_control_executor  # noqa: SLF001


def test_profile_growth_summary_uses_control_plane_executor():
    executor = ws._executor_for_request(  # noqa: SLF001
        {"id": "1", "method": "profile.growth.summary", "params": {"agentProfileId": "agent-a"}}
    )

    assert executor is ws._ws_control_executor  # noqa: SLF001


def test_conversation_render_snapshot_uses_control_plane_executor():
    executor = ws._executor_for_request(  # noqa: SLF001
        {"id": "1", "method": "conversation.render_snapshot", "params": {"session_id": "stored-1"}}
    )

    assert executor is ws._ws_control_executor  # noqa: SLF001


@pytest.mark.parametrize(
    "method",
    [
        "run.list",
        "run.status",
    ],
)
def test_profile_scoped_runtime_read_methods_are_proxied_to_runtime_worker(method):
    """``run.list`` / ``run.status`` read per-profile run state; they're
    re-proxied to the worker until Phase 4-5 ports them over the
    stdin/stdout protocol."""
    assert runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": method,
            "params": {
                "stored_session_id": "stored-session-1",
                "runtime_scope_key": "profile:agent-a:version:v1",
                "dovie_profile": {
                    "id": "agent-a",
                    "runtimeScopeKey": "profile:agent-a:version:v1",
                    "agentProfileVersionId": "v1",
                    "hermesHomePath": "/tmp/hermes-agent-a/.dovie/versions/v1",
                },
            },
        }
    )


@pytest.mark.parametrize(
    "method",
    [
        "events.subscribe",
        "events.unsubscribe",
        "run.events",
    ],
)
def test_event_read_methods_stay_on_control_plane(method):
    """``events.subscribe``, ``events.unsubscribe`` and ``run.events`` were
    historically routed to the worker so its local broadcaster could replay
    events to the subscriber. In the current architecture every worker
    frame is persisted to the MAIN db via the runtime bridge and broadcast
    from the MAIN gateway — the in-process handler reads from the same db,
    sees the same events, and never has to wait on a cold worker spawn.

    Routing these to the worker had two real costs we've already paid for
    in production:
      1) ``events.subscribe`` on a brand-new team conversation would force
         a leader worker spawn before the user had typed anything (no
         ``dovie_profile`` in flight → ``ensure_worker`` errored with
         ``runtime profile hermes home required``).
      2) Every frontend reconnect issued ``events.subscribe`` then a
         ``run.events`` backfill; the latter still proxied, triggering a
         10-20s cold worker spawn that read as "运行中" hanging after the
         user clicked a clarify option / switched conversations.

    Pin the rule: these three methods MUST stay in-process regardless of
    scope (profile-scoped, team-leader-scoped, anything). If you need to
    re-route one to the worker, you also need to either (a) prove the
    worker now has events the main db doesn't, or (b) gate the proxy on
    "worker already exists" so reconnect doesn't cold-spawn.
    """
    assert not runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": method,
            "params": {
                "stored_session_id": "stored-session-1",
                "runtime_scope_key": "profile:agent-a:version:v1",
                "dovie_profile": {
                    "id": "agent-a",
                    "runtimeScopeKey": "profile:agent-a:version:v1",
                    "agentProfileVersionId": "v1",
                    "hermesHomePath": "/tmp/hermes-agent-a/.dovie/versions/v1",
                },
            },
        }
    )


@pytest.mark.parametrize(
    "method",
    [
        "team_mission.node.update",
        "team_mission.plan.approve",
        "team_mission.plan.reject",
        "team_mission.cancel",
        "team_mission.schedule.ready",
    ],
)
def test_team_mission_scoped_methods_are_proxied_to_runtime_worker(method):
    """Team-mission writes touch in-worker scheduler state — they're
    proxied until the Phase 4-5 stdin/stdout worker protocol forwards
    these operations over its own command channel."""
    assert runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": method,
            "params": {
                "mission_id": "mission-1",
                "conversation_id": "conversation-1",
                "runtime_scope_key": "team:conversation-1:leader-conversation",
                "profile_runtime_scope_key": "profile:agent-a:version:v1",
                "dovie_profile": {
                    "id": "agent-a",
                    "runtimeScopeKey": "profile:agent-a:version:v1",
                    "agentProfileVersionId": "v1",
                    "hermesHomePath": "/tmp/hermes-agent-a/.dovie/versions/v1",
                },
            },
        }
    )


def test_clarify_respond_with_local_pending_stays_on_control_plane():
    """The team leader conversation runs IN the control-plane process
    (team_mission.message.submit calls run.submit in-process), so its
    clarify pending is registered locally. ``_interactive_respond_is_local``
    checks the local clarify_gateway registry by request_id and keeps the
    response in-process when the pending is here. An unknown request_id
    (a member-node clarify living in a worker process) still proxies."""
    from tools import clarify_gateway

    clarify_id = "test-clarify-local-1"
    clarify_gateway.register(clarify_id, session_key="team-session-x", question="q?", choices=["a", "b"])
    try:
        assert not runtime_proxy.should_proxy_to_runtime(
            {
                "id": "1",
                "method": "clarify.respond",
                "params": {
                    "request_id": clarify_id,
                    "answer": "a",
                    "runtime_scope_key": "team:conversation-1:leader-conversation",
                },
            }
        )
        # An unknown request_id (a member-node clarify whose pending
        # lives in a worker process) still proxies to the worker that
        # owns it — until Phase 4-5 replaces this with the stdin/stdout
        # forwarding protocol.
        assert runtime_proxy.should_proxy_to_runtime(
            {
                "id": "2",
                "method": "clarify.respond",
                "params": {
                    "request_id": "not-registered-here",
                    "answer": "a",
                    "runtime_scope_key": "team:conversation-1:leader-conversation",
                },
            }
        )
    finally:
        with clarify_gateway._lock:
            clarify_gateway._entries.pop(clarify_id, None)
            clarify_gateway._session_index.pop("team-session-x", None)


@pytest.mark.parametrize(
    "method",
    [
        "conversation.render_snapshot",
        "team_mission.graph",
        "team_mission.graph.reduce",
        "team_mission.events",
    ],
)
def test_team_mission_persisted_state_methods_stay_on_control_plane(method):
    assert not runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": method,
            "params": {
                "mission_id": "mission-1",
                "conversation_id": "conversation-1",
                "runtime_scope_key": "team:conversation-1:leader-conversation",
                "agent_profile_id": "agent-a",
                "agent_profile_version_id": "v1",
            },
        }
    )


@pytest.mark.parametrize(
    "method",
    [
        "events.subscribe",
        "run.events",
        "run.status",
        "session.messages",
    ],
)
def test_unscoped_runtime_read_methods_stay_on_control_plane(method):
    assert not runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": method,
            "params": {
                "stored_session_id": "stored-session-1",
            },
        }
    )


def test_prompt_submit_with_profile_scope_is_proxied_to_runtime_worker():
    assert runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": "prompt.submit",
            "params": {
                "runtime_scope_key": "profile:agent-a",
                "dovie_profile": {
                    "id": "agent-a",
                    "hermesHomePath": "/tmp/hermes-agent-a",
                },
            },
        }
    )


@pytest.mark.parametrize("method", ["team_mission.message.submit", "team_mission.conversation.ensure"])
def test_team_leader_runtime_methods_do_not_proxy_from_profile_or_member_payload(method):
    assert not runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": method,
            "params": {
                "conversation_id": "conversation-1",
                "conversation_session_id": "team-session-1",
                "runtimeScopeKey": "team:conversation-1:leader-conversation",
                "profileRuntimeScopeKey": "profile:agent-leader:version:version-leader",
                "agentProfileId": "agent-leader",
                "agentProfileVersionId": "version-leader",
                "dovie_profile": {
                    "id": "agent-leader",
                    "agentProfileVersionId": "version-leader",
                    "runtimeScopeKey": "profile:agent-leader:version:version-leader",
                    "hermesHomePath": "/tmp/hermes-agent-leader",
                },
                "members": [
                    {
                        "role": "lead",
                        "profile_id": "agent-leader",
                        "profile_version_id": "version-leader",
                        "runtime_scope_key": "profile:agent-leader:version:version-leader",
                        "hermes_home_path": "/tmp/hermes-agent-leader",
                    }
                ],
            },
        }
    )


@pytest.mark.parametrize("method", ["team_mission.message.submit", "team_mission.conversation.ensure"])
def test_team_conversation_canonical_write_methods_stay_on_control_plane_after_runtime_context_resolution(method):
    assert not runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": method,
            "params": {
                "conversation_id": "conversation-1",
                "conversation_session_id": "team-session-1",
                "runtime_scope_key": "team:conversation-1:leader-conversation",
                "agent_profile_id": "agent-leader",
                "dovie_profile": {
                    "id": "agent-leader",
                    "runtimeScopeKey": "profile:agent-leader",
                    "hermesHomePath": "/tmp/hermes-agent-leader",
                },
            },
        },
        resolve_team_context=False,
    )


def test_team_mission_create_does_not_proxy_from_nested_members():
    assert not runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": "team_mission.create",
            "params": {
                "members": [
                    {
                        "role": "lead",
                        "profile_id": "agent-leader",
                        "profile_version_id": "version-leader",
                        "runtime_scope_key": "profile:agent-leader:version:version-leader",
                        "hermes_home_path": "/tmp/hermes-agent-leader",
                    }
                ],
            },
        }
    )


def test_runtime_worker_exit_terminalizes_scope_active_runs(monkeypatch):
    from tui_gateway import server
    from tui_gateway.services import run_control

    class ExitedProcess:
        pid = 12345

        def poll(self):
            return 1

    worker = runtime_proxy.RuntimeWorker(
        scope=runtime_proxy.RuntimeScope(
            runtime_scope_key="team:conversation-1:leader-conversation",
            hermes_home="/tmp/hermes",
        ),
        process=ExitedProcess(),
        port=1234,
        token="token",
        created_at=time.time(),
        last_started_at=time.time(),
        last_used_at=time.time(),
    )
    published = []

    monkeypatch.setattr(server, "_get_db", lambda: "db")
    monkeypatch.setattr(
        run_control,
        "list_runs",
        lambda *args, **kwargs: [
            {
                "run_id": "run-1",
                "session_id": "team-session-1",
                "turn_id": "turn-1",
                "runtime_scope_key": "team:conversation-1:leader-conversation",
                "runtime_session_id": "runtime-1",
                "status": "running",
            }
        ],
    )

    def publish_terminal(**kwargs):
        published.append(kwargs)
        return {"type": "message.complete"}

    monkeypatch.setattr(run_control, "publish_run_terminal_event", publish_terminal)

    failed = runtime_proxy._terminalize_worker_active_runs(  # noqa: SLF001
        worker,
        reason="runtime websocket closed",
    )

    assert failed == 1
    assert published[0]["stored_session_id"] == "team-session-1"
    assert published[0]["run_id"] == "run-1"
    assert published[0]["status"] == "failed"
    assert "scope=team:conversation-1:leader-conversation" in published[0]["message"]

    assert runtime_proxy._terminalize_worker_active_runs(worker, reason="again") == 0  # noqa: SLF001


def test_runtime_worker_exit_terminalizes_runs_owned_by_dead_pid_even_when_scope_mismatched(monkeypatch):
    from tui_gateway import server
    from tui_gateway.services import run_control

    class ExitedProcess:
        pid = 12345

        def poll(self):
            return 1

    worker = runtime_proxy.RuntimeWorker(
        scope=runtime_proxy.RuntimeScope(
            runtime_scope_key="profile:agent-default",
            hermes_home="/tmp/hermes",
        ),
        process=ExitedProcess(),
        port=1234,
        token="token",
        created_at=time.time(),
        last_started_at=time.time(),
        last_used_at=time.time(),
    )
    published = []

    monkeypatch.setattr(server, "_get_db", lambda: "db")

    def list_runs(*args, **kwargs):
        if kwargs.get("runtime_scope_key"):
            return []
        return [
            {
                "run_id": "run-1",
                "session_id": "team-session-1",
                "turn_id": "turn-1",
                "runtime_scope_key": "team:conversation-1:leader-conversation",
                "runtime_session_id": "runtime-1",
                "status": "running",
                "metadata": {"gateway_pid": 12345},
            }
        ]

    monkeypatch.setattr(run_control, "list_runs", list_runs)
    monkeypatch.setattr(run_control, "publish_run_terminal_event", lambda **kwargs: published.append(kwargs))

    failed = runtime_proxy._terminalize_worker_active_runs(  # noqa: SLF001
        worker,
        reason="runtime websocket closed",
    )

    assert failed == 1
    assert published[0]["runtime_scope_key"] == "team:conversation-1:leader-conversation"


def test_relayed_runtime_terminal_event_updates_owner_team_mission_db(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from tui_gateway import server
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(mission_id="mission-1", title="Mission", mode="supervised_mission")
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-verifier",
        kind="verifier",
        title="Verifier",
        status="running",
        runtime_scope_key="profile:agent-7:version:v1",
    )
    db.upsert_run(
        run_id="run-verifier",
        session_id="team:mission-1:node:node-verifier",
        runtime_scope_key="profile:agent-7:version:v1",
        turn_id="turn-verifier",
        runtime_session_id="runtime-verifier",
        status="running",
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-verifier",
        run_id="run-verifier",
        session_id="team:mission-1:node:node-verifier",
        runtime_session_id="runtime-verifier",
        runtime_scope_key="profile:agent-7:version:v1",
        role="verifier",
    )
    db.append_run_event(
        "team:mission-1:node:node-verifier",
        {
            "type": "mission.node.started",
            "session_id": "runtime-verifier",
            "stored_session_id": "team:mission-1:node:node-verifier",
            "run_id": "run-verifier",
            "turn_id": "turn-verifier",
            "runtime_scope_key": "profile:agent-7:version:v1",
            "seq": 331,
            "payload": {"node_id": "node-verifier"},
        },
    )
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_db_for_stable_session", lambda _stable: db)

    relayed_complete = {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {
            "type": "message.complete",
            "session_id": "runtime-verifier",
            "stored_session_id": "team:mission-1:node:node-verifier",
            "run_id": "run-verifier",
            "turn_id": "turn-verifier",
            "runtime_scope_key": "profile:agent-7:version:v1",
            "seq": 331,
            "payload": {
                "status": "complete",
                "text": "verification passed",
            },
        },
    }

    runtime_proxy._persist_relayed_runtime_event(relayed_complete)  # noqa: SLF001

    node = db.get_team_mission_node("mission-1", "node-verifier")
    run = db.get_run("run-verifier")
    assert node["status"] == "completed"
    assert node["metadata"]["last_run_terminal_status"] == "completed"
    assert node["metadata"]["last_run_terminal_seq"] > 331
    assert run["status"] == "completed"

    runtime_proxy._persist_relayed_runtime_event(relayed_complete)  # noqa: SLF001
    completed_events = [
        event for event in db.list_team_mission_run_events("mission-1")
        if event["type"] == "team_mission.runtime.event"
        and (event.get("payload") or {}).get("event_type") == "message.complete"
        and (((event.get("payload") or {}).get("source_event") or {}).get("payload") or {}).get("status")
        == "complete"
        and (((event.get("payload") or {}).get("source_event") or {}).get("payload") or {}).get(
            "runtime_source_seq"
        )
        == 331
    ]
    assert len(completed_events) == 1

    run_control.publish_run_terminal_event(
        stored_session_id="team:mission-1:node:node-verifier",
        run_id="run-verifier",
        turn_id="turn-verifier",
        runtime_scope_key="profile:agent-7:version:v1",
        runtime_session_id="runtime-verifier",
        status="failed",
        message="prompt worker terminal event did not close active run",
        db=db,
    )

    node = db.get_team_mission_node("mission-1", "node-verifier")
    run = db.get_run("run-verifier")
    assert node["status"] == "completed"
    assert node["metadata"]["last_run_terminal_status"] == "completed"
    assert run["status"] == "completed"


def test_team_mission_emit_uses_control_db_while_profile_context_is_active(tmp_path, monkeypatch):
    from hermes_state import SessionDB
    from tui_gateway import server

    control_home = tmp_path / "control"
    profile_home = tmp_path / "profile"
    control_home.mkdir()
    profile_home.mkdir()
    control_db = SessionDB(control_home / "state.db")
    profile_db = SessionDB(profile_home / "state.db")
    stable = "team:mission-1:node:node-verifier"
    runtime_sid = "runtime-verifier"
    run_id = "run-verifier"

    control_db.create_session(stable, source="team_mission", transient=False)
    control_db.upsert_run(
        run_id=run_id,
        session_id=stable,
        runtime_scope_key="profile:agent-7",
        turn_id="turn-verifier",
        runtime_session_id=runtime_sid,
        status="running",
    )

    monkeypatch.setattr(server, "_hermes_home", str(control_home))
    monkeypatch.setattr(server, "_db", control_db)
    monkeypatch.setattr(server, "_db_error", None)
    monkeypatch.setattr(server, "_db_by_home", {str(profile_home.resolve()): profile_db})
    monkeypatch.setattr(server, "_db_error_by_home", {})

    with server._sessions_lock:
        server._sessions[runtime_sid] = {
            "session_key": stable,
            "active_run_id": run_id,
            "active_turn_id": "turn-verifier",
            "runtime_scope_key": "profile:agent-7",
            "transport": None,
        }

    token = server._enter_profile_context(
        {
            "id": "agent-7",
            "hermes_home": str(profile_home),
            "runtime_scope_key": "profile:agent-7",
        }
    )
    try:
        server._emit(
            "message.complete",
            runtime_sid,
            {
                "status": "complete",
                "text": "verification passed",
            },
        )
    finally:
        server._leave_profile_context(token)
        with server._sessions_lock:
            server._sessions.pop(runtime_sid, None)

    assert control_db.get_run(run_id)["status"] == "completed"
    assert [
        event["type"]
        for event in control_db.list_run_events(stable, run_id=run_id)
    ] == ["message.complete"]
    assert profile_db.get_run(run_id) is None
    assert profile_db.list_run_events(stable, run_id=run_id) == []


def test_team_mission_agent_uses_control_db_while_profile_context_is_active(tmp_path, monkeypatch):
    import hermes_cli.runtime_provider as runtime_provider
    import run_agent
    from hermes_state import SessionDB
    from tui_gateway import server

    control_home = tmp_path / "control"
    profile_home = tmp_path / "profile"
    control_home.mkdir()
    profile_home.mkdir()
    control_db = SessionDB(control_home / "state.db")
    profile_db = SessionDB(profile_home / "state.db")
    stable = "team-session-team-conversation-1"
    captured: dict[str, object] = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.session_id = kwargs.get("session_id")

    monkeypatch.setattr(server, "_hermes_home", str(control_home))
    monkeypatch.setattr(server, "_db", control_db)
    monkeypatch.setattr(server, "_db_error", None)
    monkeypatch.setattr(server, "_db_by_home", {str(profile_home.resolve()): profile_db})
    monkeypatch.setattr(server, "_db_error_by_home", {})
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_resolve_startup_runtime", lambda: ("test-model", "test-provider"))
    monkeypatch.setattr(runtime_provider, "resolve_runtime_provider", lambda **_kwargs: {"provider": "test-provider"})
    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)

    token = server._enter_profile_context(
        {
            "id": "agent-7",
            "hermes_home": str(profile_home),
            "runtime_scope_key": "profile:agent-7",
        }
    )
    try:
        server._make_agent("runtime-sid", stable, session_id=stable)
    finally:
        server._leave_profile_context(token)

    assert captured["session_db"] is control_db
    assert captured["session_db"] is not profile_db


def test_run_control_does_not_deliver_duplicate_terminal_events(tmp_path):
    from hermes_state import SessionDB
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    delivered = []

    class CapturingTransport:
        def write(self, obj):
            delivered.append(obj)
            return True

    subscription_id, _replay = run_control.subscribe_session_with_id(
        stored_session_id="stored-terminal-dedupe",
        transport=CapturingTransport(),
        db=db,
    )
    try:
        for seq in (1, 2):
            run_control.publish_recorded_event(
                {
                    "type": "message.complete",
                    "session_id": "runtime-terminal-dedupe",
                    "stored_session_id": "stored-terminal-dedupe",
                    "run_id": "run-terminal-dedupe",
                    "turn_id": "turn-terminal-dedupe",
                    "runtime_scope_key": "profile:agent-default",
                    "seq": seq,
                    "payload": {"status": "complete", "text": f"done {seq}"},
                },
                db=db,
            )
    finally:
        run_control.unsubscribe_session(subscription_id=subscription_id)
        db.close()

    delivered_events = [
        item.get("params") or {}
        for item in delivered
        if item.get("method") == "event"
    ]

    assert [event["type"] for event in delivered_events] == ["message.complete"]
    assert delivered_events[0]["payload"]["text"] == "done 1"


def test_run_control_does_not_deliver_stream_events_after_terminal(tmp_path):
    from hermes_state import SessionDB
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    delivered = []

    class CapturingTransport:
        def write(self, obj):
            delivered.append(obj)
            return True

    subscription_id, _replay = run_control.subscribe_session_with_id(
        stored_session_id="stored-terminal-boundary",
        transport=CapturingTransport(),
        db=db,
    )
    try:
        run_control.publish_recorded_event(
            {
                "type": "message.complete",
                "session_id": "runtime-terminal-boundary",
                "stored_session_id": "stored-terminal-boundary",
                "run_id": "run-terminal-boundary",
                "turn_id": "turn-terminal-boundary",
                "runtime_scope_key": "profile:agent-default",
                "seq": 1,
                "payload": {"status": "complete", "text": "done"},
            },
            db=db,
        )
        run_control.publish_recorded_event(
            {
                "type": "message.delta",
                "session_id": "runtime-terminal-boundary",
                "stored_session_id": "stored-terminal-boundary",
                "run_id": "run-terminal-boundary",
                "turn_id": "turn-terminal-boundary",
                "runtime_scope_key": "profile:agent-default",
                "seq": 2,
                "payload": {"text": "late"},
            },
            db=db,
        )
        active_runs = run_control.list_runs(
            "stored-terminal-boundary",
            db=db,
            statuses=sorted(run_control.ACTIVE_RUN_STATUSES),
        )
        run_state = run_control.get_run("run-terminal-boundary", db=db)
    finally:
        run_control.unsubscribe_session(subscription_id=subscription_id)
        db.close()

    delivered_events = [
        item.get("params") or {}
        for item in delivered
        if item.get("method") == "event"
    ]

    assert [event["type"] for event in delivered_events] == ["message.complete"]
    assert delivered_events[0]["payload"]["text"] == "done"
    assert active_runs == []
    assert run_state["status"] == "completed"


def test_run_control_live_publish_preserves_append_stream_delta_chunks(tmp_path):
    from hermes_state import SessionDB
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    delivered = []

    class CapturingTransport:
        def write(self, obj):
            delivered.append(obj)
            return True

    subscription_id, _replay = run_control.subscribe_session_with_id(
        stored_session_id="stored-coalesced-live",
        transport=CapturingTransport(),
        db=db,
    )
    try:
        run_control.publish_recorded_event(
            {
                "type": "message.delta",
                "session_id": "runtime-coalesced-live",
                "stored_session_id": "stored-coalesced-live",
                "run_id": "run-coalesced-live",
                "turn_id": "turn-coalesced-live",
                "runtime_scope_key": "profile:agent-default",
                "seq": 1,
                "payload": {
                    "mode": "append",
                    "text": "你",
                    "delta": "你",
                    "offset": 0,
                },
            },
            db=db,
        )
        run_control.publish_recorded_event(
            {
                "type": "message.delta",
                "session_id": "runtime-coalesced-live",
                "stored_session_id": "stored-coalesced-live",
                "run_id": "run-coalesced-live",
                "turn_id": "turn-coalesced-live",
                "runtime_scope_key": "profile:agent-default",
                "seq": 2,
                "payload": {
                    "mode": "append",
                    "text": "好",
                    "delta": "好",
                    "offset": 1,
                },
            },
            db=db,
        )
    finally:
        run_control.unsubscribe_session(subscription_id=subscription_id)
        db.close()

    delta_payloads = [
        (item.get("params") or {}).get("payload") or {}
        for item in delivered
        if (item.get("params") or {}).get("type") == "message.delta"
    ]

    assert [payload.get("delta") for payload in delta_payloads] == ["你", "好"]
    assert [payload.get("text") for payload in delta_payloads] == ["你", "好"]
    assert delta_payloads[-1].get("offset") == 1


def test_run_control_fans_out_team_mission_activity_event_after_persist(tmp_path):
    from hermes_state import SessionDB
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    delivered = []

    class CapturingTransport:
        def write(self, obj):
            delivered.append(obj)
            return True

    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        title="Mission",
        mode="autonomous_mission",
        leader_session_id="team-session-1",
        metadata={"task_id": "task-1", "stableTeamSessionId": "team-session-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        status="running",
        metadata={"task_id": "task-1"},
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="session-worker",
        runtime_session_id="runtime-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        role="worker",
    )

    subscription_id, replay = run_control.subscribe_activity(
        activity_id="mission:mission-1",
        transport=CapturingTransport(),
        db=db,
    )
    assert replay == []
    try:
        run_control.publish_recorded_event(
            {
                "type": "message.delta",
                "session_id": "runtime-worker",
                "stored_session_id": "session-worker",
                "run_id": "run-worker",
                "runtime_scope_key": "team:mission-1:node:node-worker",
                "activity_id": "mission:mission-1",
                "seq": 1,
                "payload": {
                    "activity_id": "mission:mission-1",
                    "delta": "live token",
                    "mode": "append",
                },
            },
            db=db,
        )
    finally:
        run_control.unsubscribe_activity(subscription_id=subscription_id)
        db.close()

    delivered_events = [
        item.get("params") or {}
        for item in delivered
        if item.get("method") == "event"
    ]
    assert len(delivered_events) == 1
    assert delivered_events[0]["type"] == "message.delta"
    assert delivered_events[0]["activity_id"] == "mission:mission-1"
    assert delivered_events[0]["payload"]["delta"] == "live token"


def test_run_control_session_subscription_ignores_team_mission_projection_events(tmp_path):
    from hermes_state import SessionDB
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    delivered = []

    class CapturingTransport:
        def write(self, obj):
            delivered.append(obj)
            return True

    subscription_id, _replay = run_control.subscribe_session_with_id(
        stored_session_id="team-session-1",
        transport=CapturingTransport(),
        db=db,
    )
    try:
        run_control.publish_recorded_event(
            {
                "type": "team_mission.runtime.event",
                "session_id": "runtime-team-session",
                "stored_session_id": "team-session-1",
                "stable_session_id": "team-session-1",
                "mission_id": "mission-1",
                "run_id": "run-worker",
                "turn_id": "turn-worker",
                "seq": 1,
                "payload": {
                    "protocol": "team_mission.event.v1",
                    "kind": "node.output.delta",
                    "source_event_type": "message.delta",
                    "text_stream": {"mode": "append", "delta": "mission-only", "offset": 0},
                },
            },
            db=db,
        )
        run_control.publish_recorded_event(
            {
                "type": "message.delta",
                "session_id": "runtime-team-session",
                "stored_session_id": "team-session-1",
                "run_id": "run-leader",
                "turn_id": "turn-leader",
                "runtime_scope_key": "team:conversation:leader",
                "seq": 2,
                "payload": {"mode": "append", "text": "leader", "delta": "leader", "offset": 0},
            },
            db=db,
        )
    finally:
        run_control.unsubscribe_session(subscription_id=subscription_id)
        db.close()

    delivered_events = [
        item.get("params") or {}
        for item in delivered
        if item.get("method") == "event"
    ]
    assert [event["type"] for event in delivered_events] == ["message.delta"]
    assert delivered_events[0]["payload"]["delta"] == "leader"


def test_run_control_replaces_duplicate_session_subscriptions_per_transport(tmp_path):
    from hermes_state import SessionDB
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    delivered = []

    class CapturingTransport:
        def write(self, obj):
            delivered.append(obj)
            return True

    transport = CapturingTransport()
    first_subscription_id, _ = run_control.subscribe_session_with_id(
        stored_session_id="stored-duplicate-session",
        transport=transport,
        db=db,
    )
    second_subscription_id, _ = run_control.subscribe_session_with_id(
        stored_session_id="stored-duplicate-session",
        transport=transport,
        db=db,
    )
    try:
        run_control.publish_recorded_event(
            {
                "type": "message.delta",
                "session_id": "runtime-duplicate-session",
                "stored_session_id": "stored-duplicate-session",
                "run_id": "run-duplicate-session",
                "turn_id": "turn-duplicate-session",
                "runtime_scope_key": "profile:agent-default",
                "seq": 1,
                "payload": {"mode": "append", "text": "one", "delta": "one", "offset": 0},
            },
            db=db,
        )
    finally:
        run_control.unsubscribe_session(subscription_id=first_subscription_id)
        run_control.unsubscribe_session(subscription_id=second_subscription_id)
        db.close()

    delivered_events = [
        item.get("params") or {}
        for item in delivered
        if item.get("method") == "event"
    ]
    assert [event["type"] for event in delivered_events] == ["message.delta"]
    assert delivered_events[0]["payload"]["delta"] == "one"


def test_run_control_replaces_duplicate_activity_subscriptions_per_transport(tmp_path):
    from hermes_state import SessionDB
    from tui_gateway.services import run_control

    db = SessionDB(tmp_path / "state.db")
    delivered = []

    class CapturingTransport:
        def write(self, obj):
            delivered.append(obj)
            return True

    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        title="Mission",
        mode="autonomous_mission",
        leader_session_id="team-session-1",
        metadata={"task_id": "task-1", "stableTeamSessionId": "team-session-1"},
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        status="running",
        metadata={"task_id": "task-1"},
    )
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-worker",
        session_id="session-worker",
        runtime_session_id="runtime-worker",
        runtime_scope_key="team:mission-1:node:node-worker",
        role="worker",
    )

    transport = CapturingTransport()
    first_subscription_id, _ = run_control.subscribe_activity(
        activity_id="mission:mission-1",
        transport=transport,
        db=db,
    )
    second_subscription_id, _ = run_control.subscribe_activity(
        activity_id="mission:mission-1",
        transport=transport,
        db=db,
    )
    try:
        run_control.publish_recorded_event(
            {
                "type": "message.delta",
                "session_id": "runtime-worker",
                "stored_session_id": "session-worker",
                "run_id": "run-worker",
                "runtime_scope_key": "team:mission-1:node:node-worker",
                "activity_id": "mission:mission-1",
                "seq": 1,
                "payload": {
                    "activity_id": "mission:mission-1",
                    "delta": "live token",
                    "mode": "append",
                },
            },
            db=db,
        )
    finally:
        run_control.unsubscribe_activity(subscription_id=first_subscription_id)
        run_control.unsubscribe_activity(subscription_id=second_subscription_id)
        db.close()

    delivered_events = [
        item.get("params") or {}
        for item in delivered
        if item.get("method") == "event"
    ]
    assert [event["type"] for event in delivered_events] == ["message.delta"]
    assert delivered_events[0]["activity_id"] == "mission:mission-1"
    assert delivered_events[0]["payload"]["delta"] == "live token"


def test_control_plane_session_list_is_not_proxied_to_runtime_worker():
    assert not runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": "session.list",
            "params": {
                "runtime_scope_key": "profile:agent-a",
                "dovie_profile": {
                    "id": "agent-a",
                    "hermesHomePath": "/tmp/hermes-agent-a",
                },
            },
        }
    )


def test_control_plane_session_title_is_not_proxied_to_runtime_worker():
    assert not runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": "session.title",
            "params": {
                "stored_session_id": "stored-session-1",
                "runtime_scope_key": "profile:agent-a:version:v1",
                "dovie_profile": {
                    "id": "agent-a",
                    "runtimeScopeKey": "profile:agent-a:version:v1",
                    "agentProfileVersionId": "v1",
                    "hermesHomePath": "/tmp/hermes-agent-a/.dovie/versions/v1",
                },
            },
        }
    )


def test_control_plane_session_messages_are_not_proxied_to_runtime_worker():
    assert not runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": "session.messages",
            "params": {
                "stored_session_id": "stored-session-1",
                "runtime_scope_key": "team:conversation-1:leader-conversation",
                "profile_runtime_scope_key": "profile:agent-a:version:v1",
                "dovie_profile": {
                    "id": "agent-a",
                    "runtimeScopeKey": "profile:agent-a:version:v1",
                    "agentProfileVersionId": "v1",
                    "hermesHomePath": "/tmp/hermes-agent-a/.dovie/versions/v1",
                },
            },
        }
    )


def test_control_plane_team_mission_node_history_is_not_proxied_to_runtime_worker():
    assert not runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": "team_mission.node.history",
            "params": {
                "mission_id": "mission-1",
                "node_id": "node-worker",
                "runtime_scope_key": "team:conversation-1:leader-conversation",
                "profile_runtime_scope_key": "profile:agent-a:version:v1",
                "dovie_profile": {
                    "id": "agent-a",
                    "runtimeScopeKey": "profile:agent-a:version:v1",
                    "agentProfileVersionId": "v1",
                    "hermesHomePath": "/tmp/hermes-agent-a/.dovie/versions/v1",
                },
            },
        }
    )


def test_profile_scoped_cron_manage_is_proxied_to_runtime_worker():
    """cron.manage default (no controlPlaneOnly) writes cron jobs into
    the worker's in-memory cron runtime — proxy until Phase 4-5
    forwards cron ops over the new worker protocol."""
    assert runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": "cron.manage",
            "params": {
                "action": "list",
                "dovie_profile": {
                    "id": "agent-a",
                    "runtimeScopeKey": "profile:agent-a",
                    "hermesHomePath": "/tmp/hermes-agent-a",
                },
            },
        }
    )


@pytest.mark.parametrize("action", ["list", "status", "runs", ""])
def test_profile_scoped_cron_control_plane_reads_are_not_proxied_to_runtime_worker(action):
    assert not runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": "cron.manage",
            "params": {
                "action": action,
                "controlPlaneOnly": True,
                "dovie_profile": {
                    "id": "agent-a",
                    "runtimeScopeKey": "profile:agent-a:version:v1",
                    "agentProfileVersionId": "v1",
                    "hermesHomePath": "/tmp/hermes-agent-a/.dovie/versions/v1",
                },
            },
        }
    )


def test_profile_growth_summary_stays_on_control_plane_with_profile_scope():
    assert not runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": "profile.growth.summary",
            "params": {
                "agentProfileId": "agent-a",
                "agentProfileVersionId": "v1",
                "runtime_scope_key": "profile:agent-a:version:v1",
                "controlPlaneOnly": True,
                "dovie_profile": {
                    "id": "agent-a",
                    "runtimeScopeKey": "profile:agent-a:version:v1",
                    "agentProfileVersionId": "v1",
                    "hermesHomePath": "/tmp/hermes-agent-a/.dovie/versions/v1",
                },
            },
        }
    )


@pytest.mark.parametrize("action", ["add", "update", "remove", "run", "pause", "resume"])
def test_profile_scoped_cron_control_plane_flag_does_not_bypass_runtime_mutations(action):
    """Cron mutations target the worker's cron runtime regardless of
    the ``controlPlaneOnly`` hint — that hint is only honored for the
    read-only list/status actions. Mutations proxy until Phase 4-5."""
    assert runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": "cron.manage",
            "params": {
                "action": action,
                "controlPlaneOnly": True,
                "dovie_profile": {
                    "id": "agent-a",
                    "runtimeScopeKey": "profile:agent-a:version:v1",
                    "agentProfileVersionId": "v1",
                    "hermesHomePath": "/tmp/hermes-agent-a/.dovie/versions/v1",
                },
            },
        }
    )


@pytest.mark.parametrize(
    "method",
    [
        "approval.policy.get",
        "approval.policy.set",
    ],
)
def test_interactive_respond_methods_stay_on_control_plane(method):
    """Phase 3 of sub-sidecar removal: clarify.respond / approval.respond
    / secret.respond / sudo.respond / approval.pending.list operate on
    pure in-memory state (``tools/clarify_gateway.py:_entries`` etc.)
    that is now keyed by ProfileContext. Proxying them to a worker
    process used to land them in the wrong sub map (the worker's own
    ``_gateway_queues``, never written to from the main side), causing
    the "clarify response triggered but no message.delta appeared"
    bug. Pin the new contract: control plane handles them in-process."""
    assert not runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": method,
            "params": {
                "session_id": "stored-session-1",
                "dovie_profile": {
                    "id": "agent-a",
                    "runtimeScopeKey": "profile:agent-a",
                    "hermesHomePath": "/tmp/hermes-agent-a",
                },
            },
        }
    )


@pytest.mark.parametrize(
    "method",
    [
        "skills.reload",
        "toolsets.list",
        "tools.configure",
    ],
)
def test_profile_scoped_runtime_tool_methods_are_proxied_to_runtime_worker(method):
    """skills.reload / toolsets.list / tools.configure touch in-worker
    registries (skill module map, toolset enable/disable state). They
    proxy until Phase 4-5 forwards these registry ops over the new
    worker protocol."""
    assert runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": method,
            "params": {
                "session_id": "stored-session-1",
                "dovie_profile": {
                    "id": "agent-a",
                    "runtimeScopeKey": "profile:agent-a",
                    "hermesHomePath": "/tmp/hermes-agent-a",
                },
            },
        }
    )


def test_runtime_ensure_stays_on_control_plane():
    assert not runtime_proxy.should_proxy_to_runtime(
        {
            "id": "1",
            "method": "runtime.ensure",
            "params": {
                "runtime_scope_key": "profile:agent-a",
                "dovie_profile": {
                    "id": "agent-a",
                    "hermesHomePath": "/tmp/hermes-agent-a",
                },
            },
        }
    )


def test_dovie_sidecar_host_and_origin_policy():
    from tui_gateway import dovie_sidecar

    assert dovie_sidecar.is_allowed_host("127.0.0.1:4567")
    assert dovie_sidecar.is_allowed_host("localhost:4567")
    assert dovie_sidecar.is_allowed_host("[::1]:4567")
    assert not dovie_sidecar.is_allowed_host("evil.example")

    assert dovie_sidecar.is_allowed_origin("")
    assert dovie_sidecar.is_allowed_origin(None)
    assert dovie_sidecar.is_allowed_origin("dovie://renderer")
    assert dovie_sidecar.is_allowed_origin("dovie-hermes://gateway")
    assert not dovie_sidecar.is_allowed_origin("null")
    assert not dovie_sidecar.is_allowed_origin("http://127.0.0.1:3000")
    assert not dovie_sidecar.is_allowed_origin("https://evil.example")


def test_dovie_sidecar_parent_watchdog_keeps_live_reparented_process(monkeypatch):
    from tui_gateway import dovie_sidecar

    monkeypatch.setenv(dovie_sidecar.SIDECAR_PARENT_PID_ENV, "12345")
    monkeypatch.setattr(dovie_sidecar.os, "getppid", lambda: 1)
    monkeypatch.setattr(dovie_sidecar.os, "kill", lambda pid, signal: None)

    assert dovie_sidecar.expected_parent_pid() == 12345
    assert dovie_sidecar.parent_process_still_owns_sidecar(12345)


def test_dovie_sidecar_parent_watchdog_detects_missing_parent(monkeypatch):
    from tui_gateway import dovie_sidecar

    def raise_missing(pid, signal):
        raise ProcessLookupError()

    monkeypatch.setattr(dovie_sidecar.os, "getppid", lambda: 1)
    monkeypatch.setattr(dovie_sidecar.os, "kill", raise_missing)

    assert not dovie_sidecar.parent_process_still_owns_sidecar(12345)


@pytest.mark.asyncio
async def test_runtime_proxy_keeps_bridge_open_for_streaming_events(monkeypatch):
    class FakeRuntimeSocket:
        def __init__(self):
            self.sent = []
            self.closed = False
            self.frames = asyncio.Queue()
            self.frames.put_nowait(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {"type": "gateway.ready"},
                    }
                )
            )

        async def send(self, raw):
            self.sent.append(json.loads(raw))

        async def recv(self):
            return await self.frames.get()

        async def close(self):
            self.closed = True

    class FakeClientSocket:
        def __init__(self):
            self.sent = []

        async def send_text(self, raw):
            self.sent.append(json.loads(raw))

    runtime_socket = FakeRuntimeSocket()

    async def fake_connect(_uri):
        return runtime_socket

    class FakeProcess:
        pid = 12345

        def poll(self):
            return None

    worker = runtime_proxy.RuntimeWorker(
        scope=runtime_proxy.RuntimeScope(
            agent_profile_id="agent-a",
            runtime_scope_key="profile:agent-a",
            hermes_home="/tmp/hermes-agent-a",
        ),
        process=FakeProcess(),
        port=19450,
        token="token",
        created_at=1,
        last_started_at=1,
        last_used_at=1,
    )

    class FakePool:
        async def ensure_worker(self, _scope, _params):
            return worker

        async def retain_bridge(self, scope_key):
            worker.bridge_count += 1

        async def release_bridge(self, scope_key):
            worker.bridge_count -= 1

    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=fake_connect))
    monkeypatch.setattr(runtime_proxy, "runtime_proxy_pool", lambda: FakePool())

    transport = ws.WSTransport(FakeClientSocket(), asyncio.get_running_loop())
    req = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "prompt.submit",
        "params": {
            "runtime_scope_key": "profile:agent-a",
            "dovie_profile": {
                "id": "agent-a",
                "hermesHomePath": "/tmp/hermes-agent-a",
            },
        },
    }

    assert await runtime_proxy.proxy_to_runtime(req, transport)
    assert runtime_socket.sent == [req]

    runtime_socket.frames.put_nowait(
        json.dumps({"jsonrpc": "2.0", "id": "1", "result": {"status": "streaming"}})
    )
    runtime_socket.frames.put_nowait(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "message.delta",
                    "session_id": "s",
                    "payload": {"text": "hello"},
                },
            }
        )
    )
    for _ in range(20):
        if len(transport._ws.sent) >= 2:  # noqa: SLF001
            break
        await asyncio.sleep(0.01)

    assert transport._ws.sent == [  # noqa: SLF001
        {"jsonrpc": "2.0", "id": "1", "result": {"status": "streaming"}},
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "message.delta",
                "session_id": "s",
                "payload": {"text": "hello"},
            },
        },
    ]
    assert not runtime_socket.closed

    await transport.aclose()
    assert runtime_socket.closed
    assert worker.bridge_count == 0


@pytest.mark.asyncio
async def test_runtime_proxy_retains_worker_before_bridge_creation(monkeypatch):
    class FakeProcess:
        pid = 12347

        def poll(self):
            return None

    worker = runtime_proxy.RuntimeWorker(
        scope=runtime_proxy.RuntimeScope(
            agent_profile_id="agent-a",
            runtime_scope_key="profile:agent-a",
            hermes_home="/tmp/hermes-agent-a",
        ),
        process=FakeProcess(),
        port=19451,
        token="token",
        created_at=1,
        last_started_at=1,
        last_used_at=1,
    )
    events: list[tuple[str, int]] = []

    class FakePool:
        async def ensure_worker(self, _scope, _params):
            return worker

        async def retain_bridge(self, _scope_key):
            worker.bridge_count += 1
            events.append(("retain", worker.bridge_count))

        async def release_bridge(self, _scope_key):
            worker.bridge_count -= 1
            events.append(("release", worker.bridge_count))

    class FakeBridge:
        async def send(self, _req):
            events.append(("send", worker.bridge_count))

    class FakeTransport:
        async def runtime_bridge(self, bridge_worker):
            assert bridge_worker is worker
            events.append(("runtime_bridge", worker.bridge_count))
            return FakeBridge()

    monkeypatch.setattr(runtime_proxy, "runtime_proxy_pool", lambda: FakePool())

    req = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "prompt.submit",
        "params": {
            "runtime_scope_key": "profile:agent-a",
            "dovie_profile": {
                "id": "agent-a",
                "hermesHomePath": "/tmp/hermes-agent-a",
            },
        },
    }

    assert await runtime_proxy.proxy_to_runtime(req, FakeTransport())
    assert events == [
        ("retain", 1),
        ("runtime_bridge", 1),
        ("send", 1),
        ("release", 0),
    ]
    assert worker.bridge_count == 0


@pytest.mark.asyncio
async def test_runtime_proxy_rejects_non_object_ready_frame_without_attribute_error(monkeypatch):
    class FakeRuntimeSocket:
        def __init__(self):
            self.closed = False

        async def recv(self):
            return "null"

        async def close(self):
            self.closed = True

    class FakeProcess:
        pid = 12346

        def poll(self):
            return None

    class FakePool:
        async def retain_bridge(self, _scope_key):
            raise AssertionError("bridge must not be retained before a valid ready frame")

        async def release_bridge(self, _scope_key):
            raise AssertionError("bridge was never retained")

    runtime_socket = FakeRuntimeSocket()

    async def fake_connect(_uri):
        return runtime_socket

    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=fake_connect))
    monkeypatch.setattr(runtime_proxy, "_RUNTIME_CONNECT_ATTEMPTS", 1)
    monkeypatch.setattr(runtime_proxy, "_RUNTIME_CONNECT_DELAY_S", 0)

    worker = runtime_proxy.RuntimeWorker(
        scope=runtime_proxy.RuntimeScope(
            agent_profile_id="agent-a",
            runtime_scope_key="profile:agent-a",
            hermes_home="/tmp/hermes-agent-a",
        ),
        process=FakeProcess(),
        port=19451,
        token="token",
        created_at=1,
        last_started_at=1,
        last_used_at=1,
    )
    bridge = runtime_proxy.RuntimeProxyBridge(
        worker=worker,
        transport=object(),
        pool=FakePool(),
    )

    with pytest.raises(RuntimeError, match="ready frame must be a JSON object, got NoneType"):
        await bridge.send({"id": "1", "method": "run.status", "params": {}})
    assert runtime_socket.closed
    assert bridge.closed


@pytest.mark.asyncio
async def test_ws_transport_replaces_cached_runtime_bridge_when_worker_changes():
    class FakeClientSocket:
        async def send_text(self, _raw):
            return None

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return None

    scope = runtime_proxy.RuntimeScope(
        agent_profile_id="agent-a",
        runtime_scope_key="team:conversation-1:leader-conversation",
        hermes_home="/tmp/hermes-agent-a",
    )
    first_worker = runtime_proxy.RuntimeWorker(
        scope=scope,
        process=FakeProcess(21001),
        port=21001,
        token="first",
        created_at=1,
        last_started_at=1,
        last_used_at=1,
    )
    second_worker = runtime_proxy.RuntimeWorker(
        scope=scope,
        process=FakeProcess(21002),
        port=21002,
        token="second",
        created_at=2,
        last_started_at=2,
        last_used_at=2,
    )

    transport = ws.WSTransport(FakeClientSocket(), asyncio.get_running_loop())
    first_bridge = await transport.runtime_bridge(first_worker)
    second_bridge = await transport.runtime_bridge(second_worker)

    assert second_bridge is not first_bridge
    assert first_bridge.closed
    assert second_bridge.worker is second_worker
    assert transport._runtime_bridges[scope.runtime_scope_key] is second_bridge  # noqa: SLF001


def test_runtime_proxy_ignores_non_object_requests():
    assert not runtime_proxy.should_proxy_to_runtime(None)
    assert not runtime_proxy.should_proxy_to_runtime([])
    assert not runtime_proxy.should_proxy_to_runtime(["not", "a", "request"])
    assert runtime_proxy.runtime_scope_from_request(["not", "a", "request"]) == runtime_proxy.RuntimeScope()


@pytest.mark.asyncio
async def test_runtime_worker_pool_reuses_and_reclaims_idle_workers(monkeypatch):
    class FakeProcess:
        next_pid = 20000

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.pid = FakeProcess.next_pid
            FakeProcess.next_pid += 1
            self.terminated = False
            self.killed = False

        def poll(self):
            return 0 if self.terminated or self.killed else None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, _timeout=None):
            self.terminated = True
            return 0

    now = 1000.0

    monkeypatch.setattr(runtime_proxy.time, "time", lambda: now)
    monkeypatch.setattr(runtime_proxy.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(runtime_proxy, "_reserve_loopback_port", lambda: 21000)
    monkeypatch.setenv("DOVIE_HERMES_RUNTIME_WORKER_IDLE_SECONDS", "10")

    pool = runtime_proxy.RuntimeWorkerPool()
    scope = runtime_proxy.RuntimeScope(
        agent_profile_id="agent-a",
        runtime_scope_key="profile:agent-a",
        hermes_home="/tmp/hermes-agent-a",
    )
    params = {
        "dovie_profile": {
            "id": "agent-a",
            "hermesHomePath": "/tmp/hermes-agent-a",
            "env": {"FEISHU_APP_SECRET": "secret"},
        }
    }

    first = await pool.ensure_worker(scope, params)
    second = await pool.ensure_worker(scope, params)
    assert second is first
    assert first.process.kwargs["env"]["HERMES_HOME"] == "/tmp/hermes-agent-a"
    assert first.process.kwargs["env"]["FEISHU_APP_SECRET"] == "secret"
    assert "--token" not in first.process.args[0]
    assert first.process.kwargs["env"]["DOVIE_SIDECAR_TOKEN"]
    assert first.process.kwargs["env"]["DOVIE_SIDECAR_PARENT_PID"] == str(os.getpid())

    now = 1012.0
    reclaimed = await pool.reclaim_idle()
    assert reclaimed["scopeKeys"] == ["profile:agent-a"]
    assert first.process.terminated
    assert pool.snapshot()["runningWorkerCount"] == 0


@pytest.mark.asyncio
async def test_runtime_worker_pool_restarts_when_profile_launch_env_changes(monkeypatch):
    class FakeProcess:
        next_pid = 21100

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.pid = FakeProcess.next_pid
            FakeProcess.next_pid += 1
            self.terminated = False
            self.killed = False

        def poll(self):
            return 0 if self.terminated or self.killed else None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, _timeout=None):
            self.terminated = True
            return 0

    monkeypatch.setattr(runtime_proxy.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(runtime_proxy, "_reserve_loopback_port", lambda: 21100)

    pool = runtime_proxy.RuntimeWorkerPool()
    scope = runtime_proxy.RuntimeScope(
        agent_profile_id="agent-a",
        runtime_scope_key="profile:agent-a",
        hermes_home="/tmp/hermes-agent-a",
    )
    first = await pool.ensure_worker(
        scope,
        {
            "dovie_profile": {
                "id": "agent-a",
                "hermesHomePath": "/tmp/hermes-agent-a",
                "env": {},
            }
        },
    )
    second = await pool.ensure_worker(
        scope,
        {
            "dovie_profile": {
                "id": "agent-a",
                "hermesHomePath": "/tmp/hermes-agent-a",
                "env": {
                    "DOVIE_BACKEND_BRIDGE_URL": "http://127.0.0.1:4567/api/dovie/invoke",
                    "DOVIE_BACKEND_BRIDGE_TOKEN": "bridge-token",
                },
            }
        },
    )

    assert second is not first
    assert first.process.terminated
    assert second.process.kwargs["env"]["DOVIE_BACKEND_BRIDGE_URL"] == "http://127.0.0.1:4567/api/dovie/invoke"
    assert second.process.kwargs["env"]["DOVIE_BACKEND_BRIDGE_TOKEN"] == "bridge-token"
    assert pool.snapshot()["runningWorkerCount"] == 1


@pytest.mark.asyncio
async def test_runtime_worker_pool_keeps_active_bridge_when_launch_env_changes(monkeypatch):
    class FakeProcess:
        next_pid = 21150

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.pid = FakeProcess.next_pid
            FakeProcess.next_pid += 1
            self.terminated = False

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def wait(self, _timeout=None):
            self.terminated = True
            return 0

    monkeypatch.setattr(runtime_proxy.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(runtime_proxy, "_reserve_loopback_port", lambda: 21150)

    pool = runtime_proxy.RuntimeWorkerPool()
    scope = runtime_proxy.RuntimeScope(
        agent_profile_id="agent-a",
        runtime_scope_key="team:conversation-a:leader-conversation",
        hermes_home="/tmp/hermes-agent-a",
    )
    first = await pool.ensure_worker(
        scope,
        {
            "runtime_scope_key": "team:conversation-a:leader-conversation",
            "dovie_profile": {
                "id": "agent-a",
                "hermesHomePath": "/tmp/hermes-agent-a",
                "env": {},
            },
        },
    )
    await pool.retain_bridge(first.scope_key)

    second = await pool.ensure_worker(
        scope,
        {
            "runtime_scope_key": "team:conversation-a:leader-conversation",
            "dovie_profile": {
                "id": "agent-a",
                "hermesHomePath": "/tmp/hermes-agent-a",
                "env": {
                    "DOVIE_BACKEND_BRIDGE_URL": "http://127.0.0.1:4567/api/dovie/invoke",
                },
            },
        },
    )

    assert second is first
    assert first.running()
    assert not first.process.terminated
    assert "DOVIE_BACKEND_BRIDGE_URL" not in first.process.kwargs["env"]
    assert pool.snapshot()["runningWorkerCount"] == 1


@pytest.mark.asyncio
async def test_runtime_worker_pool_keeps_active_run_when_launch_env_changes(monkeypatch):
    class FakeProcess:
        next_pid = 21170

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.pid = FakeProcess.next_pid
            FakeProcess.next_pid += 1
            self.terminated = False

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def wait(self, _timeout=None):
            self.terminated = True
            return 0

    monkeypatch.setattr(runtime_proxy.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(runtime_proxy, "_reserve_loopback_port", lambda: 21170)
    monkeypatch.setattr(runtime_proxy, "_worker_has_active_runs", lambda _worker: True)

    pool = runtime_proxy.RuntimeWorkerPool()
    scope = runtime_proxy.RuntimeScope(
        agent_profile_id="agent-a",
        runtime_scope_key="team:conversation-a:leader-conversation",
        hermes_home="/tmp/hermes-agent-a",
    )
    first = await pool.ensure_worker(
        scope,
        {
            "runtime_scope_key": "team:conversation-a:leader-conversation",
            "dovie_profile": {
                "id": "agent-a",
                "hermesHomePath": "/tmp/hermes-agent-a",
                "env": {},
            },
        },
    )

    second = await pool.ensure_worker(
        scope,
        {
            "runtime_scope_key": "team:conversation-a:leader-conversation",
            "dovie_profile": {
                "id": "agent-a",
                "hermesHomePath": "/tmp/hermes-agent-a",
                "env": {
                    "DOVIE_BACKEND_BRIDGE_TOKEN": "bridge-token",
                },
            },
        },
    )

    assert second is first
    assert first.running()
    assert not first.process.terminated
    assert "DOVIE_BACKEND_BRIDGE_TOKEN" not in first.process.kwargs["env"]
    assert pool.snapshot()["runningWorkerCount"] == 1


@pytest.mark.asyncio
async def test_runtime_worker_pool_reuses_existing_worker_when_scoped_read_lacks_launch_home(monkeypatch):
    class FakeProcess:
        next_pid = 21200

        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.pid = FakeProcess.next_pid
            FakeProcess.next_pid += 1
            self.terminated = False
            self.killed = False

        def poll(self):
            return 0 if self.terminated or self.killed else None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.killed = True

        def wait(self, _timeout=None):
            self.terminated = True
            return 0

    monkeypatch.setattr(runtime_proxy.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(runtime_proxy, "_reserve_loopback_port", lambda: 21200)

    pool = runtime_proxy.RuntimeWorkerPool()
    launch_scope = runtime_proxy.RuntimeScope(
        agent_profile_id="agent-a",
        runtime_scope_key="profile:agent-a",
        hermes_home="/tmp/hermes-agent-a",
    )
    first = await pool.ensure_worker(
        launch_scope,
        {"dovie_profile": {"id": "agent-a", "hermesHomePath": "/tmp/hermes-agent-a"}},
    )
    read_scope = runtime_proxy.RuntimeScope(
        agent_profile_id="agent-a",
        runtime_scope_key="profile:agent-a",
        hermes_home="",
    )

    second = await pool.ensure_worker(read_scope, {"runtime_scope_key": "profile:agent-a"})

    assert second is first
    assert first.running()
    assert not first.process.terminated
    assert pool.snapshot()["runningWorkerCount"] == 1


@pytest.mark.asyncio
async def test_runtime_worker_pool_does_not_reclaim_active_bridge(monkeypatch):
    class FakeProcess:
        pid = 22000

        def __init__(self, *args, **kwargs):
            self.terminated = False

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def wait(self, _timeout=None):
            self.terminated = True
            return 0

    now = 2000.0

    monkeypatch.setattr(runtime_proxy.time, "time", lambda: now)
    monkeypatch.setattr(runtime_proxy.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(runtime_proxy, "_reserve_loopback_port", lambda: 22000)
    monkeypatch.setenv("DOVIE_HERMES_RUNTIME_WORKER_IDLE_SECONDS", "10")

    pool = runtime_proxy.RuntimeWorkerPool()
    scope = runtime_proxy.RuntimeScope(
        agent_profile_id="agent-a",
        runtime_scope_key="profile:agent-a",
        hermes_home="/tmp/hermes-agent-a",
    )
    worker = await pool.ensure_worker(scope, {"dovie_profile": {"hermesHomePath": "/tmp/hermes-agent-a"}})
    await pool.retain_bridge(worker.scope_key)

    now = 2012.0
    reclaimed = await pool.reclaim_idle()
    assert reclaimed["reclaimed"] == 0
    assert worker.running()
    assert pool.snapshot()["runningWorkerCount"] == 1

    await pool.release_bridge(worker.scope_key)
    now = 2024.0
    reclaimed = await pool.reclaim_idle()
    assert reclaimed["reclaimed"] == 1
