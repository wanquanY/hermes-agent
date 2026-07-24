from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import threading
import time

from dovie_extension import DovieHermesExtension, load_extension
from dovie_extension.gateway_methods import dovie_gateway_method_overrides
from dovie_extension.manifest import gateway_capabilities as extension_gateway_capabilities
from tests.team_mission_gateway_test_support import team_mission_gateway
from tui_gateway.dovie_gateway_contract import (
    REQUIRED_METHODS,
    gateway_capabilities,
)


def test_dovie_gateway_capabilities_reports_complete_gateway_abi():
    manifest = gateway_capabilities()

    assert manifest["ok"] is True
    assert manifest["protocolVersion"] == "2026-07-24"
    for method in REQUIRED_METHODS:
        assert method in manifest["methods"]
    assert "message.delta" in manifest["events"]
    assert "runtime:dovie_sidecar" in manifest["runtimeFeatures"]
    assert "state:message_reasoning" in manifest["stateFeatures"]
    assert "state:session_search" in manifest["stateFeatures"]
    assert "state:run_registry" in manifest["stateFeatures"]
    assert "state:run_event_log" in manifest["stateFeatures"]
    assert "state:team_mission_graph" in manifest["stateFeatures"]
    assert "state:team_mission_conversation" in manifest["stateFeatures"]
    assert "state:conversation_memory" in manifest["stateFeatures"]
    assert "state:team_capability_snapshot" in manifest["stateFeatures"]
    assert "state:runtime_scope_key" in manifest["stateFeatures"]
    assert "state:transient_session" in manifest["stateFeatures"]
    assert manifest["timelineContract"] == {
        "contractVersion": "3.1",
        "capabilities": {
            "cursor": {
                "afterSeq": True,
                "afterId": True,
                "beforeSeq": True,
                "beforeId": True,
            },
            "history": {"canonical": False},
            "toolEvents": {"canonical": True},
        },
        "deprecations": [],
    }
    assert "interaction.persistent" not in json.dumps(manifest["timelineContract"])
    assert "runStateMachine.singleEntrypoint" not in json.dumps(manifest["timelineContract"])
    assert "runtimeSourceSeq" not in manifest["timelineContract"]["deprecations"]
    assert manifest["missingCapabilities"] == []


def test_dovie_gateway_manifest_does_not_probe_legacy_sessiondb():
    source = (Path(__file__).resolve().parents[1] / "dovie_extension" / "manifest.py").read_text(
        encoding="utf-8"
    )

    assert "hermes_state" not in source
    assert "SessionDB" not in source


def test_dovie_gateway_contract_is_served_by_extension_manifest():
    assert gateway_capabilities is extension_gateway_capabilities
    extension = load_extension()
    assert isinstance(extension, DovieHermesExtension)
    assert extension.register_capabilities()["extensionVersion"] == extension.version
    assert extension.gateway_method_overrides() == frozenset()
    assert extension.gateway_method_overrides() == dovie_gateway_method_overrides()


def test_gateway_capabilities_json_rpc_method_is_registered():
    import importlib

    from tui_gateway import server

    importlib.import_module("tui_gateway.methods.run")
    response = server._methods["gateway.capabilities"](1, {})

    assert response["result"]["protocolVersion"] == "2026-07-24"
    assert response["result"]["timelineContract"]["contractVersion"] == "3.1"
    assert response["result"]["timelineContract"]["capabilities"]["cursor"]["afterSeq"] is True
    assert response["result"]["timelineContract"]["capabilities"]["history"]["canonical"] is False
    assert response["result"]["timelineContract"]["capabilities"]["toolEvents"]["canonical"] is True
    assert response["result"]["timelineContract"]["deprecations"] == []
    assert "run.submit" in response["result"]["methods"]
    assert "run.retry.prepare" in response["result"]["methods"]
    assert "run.events" in response["result"]["methods"]
    assert "session.events" in response["result"]["methods"]
    assert "events.unsubscribe" in response["result"]["methods"]
    assert "conversation.render_snapshot" in response["result"]["methods"]
    assert "team_mission.create" in response["result"]["methods"]
    assert "team_mission.graph" in response["result"]["methods"]
    assert "team_mission.graph.reduce" in response["result"]["methods"]
    assert "team_mission.snapshot.get" in response["result"]["methods"]
    assert "team_mission.result.get" in response["result"]["methods"]
    assert "team_mission.events" in response["result"]["methods"]
    assert "team_capability.snapshot.get" in response["result"]["methods"]
    assert "team_capability.snapshot.refresh" in response["result"]["methods"]
    assert "team_capability.snapshot.bind" in response["result"]["methods"]
    assert "team_mission.team_profile.get" in response["result"]["methods"]
    assert "team_mission.conversation.ensure" in response["result"]["methods"]
    assert "team_mission.conversation.resolve" in response["result"]["methods"]
    assert "team_mission.conversation.render" in response["result"]["methods"]
    assert "team_mission.conversation.list" in response["result"]["methods"]
    assert "team_mission.conversation.participants" in response["result"]["methods"]
    assert "team_mission.conversation.execution_session_ids" in response["result"]["methods"]
    assert "team_mission.conversation.rename" in response["result"]["methods"]
    assert "team_mission.conversation.delete" in response["result"]["methods"]
    assert "team_mission.message.submit" in response["result"]["methods"]
    assert "team_mission.cancel" in response["result"]["methods"]
    assert "team_mission.node.create" in response["result"]["methods"]
    assert "team_mission.edge.create" in response["result"]["methods"]
    assert "team_mission.node.update" in response["result"]["methods"]
    assert "team_mission.node.bind_run" in response["result"]["methods"]
    assert "team_mission.node.history" in response["result"]["methods"]
    assert "team_mission.node.start" in response["result"]["methods"]
    assert "team_mission.plan.complete" in response["result"]["methods"]
    assert "team_mission.plan.approve" in response["result"]["methods"]
    assert "team_mission.schedule.ready" in response["result"]["methods"]
    assert "conversation.memory.propose" in response["result"]["methods"]
    assert "conversation.memory.commit" in response["result"]["methods"]
    assert "conversation.memory.invalidate" in response["result"]["methods"]
    assert "conversation.memory.list" in response["result"]["methods"]
    assert "conversation.activity.context.change" in response["result"]["methods"]
    assert "conversation.context.summary.list" in response["result"]["methods"]
    assert "session.usage" in response["result"]["methods"]
    assert "session.status" in response["result"]["methods"]
    assert "session.message_metadata.merge" in response["result"]["methods"]
    assert "prompt.submit" in response["result"]["methods"]
    assert "model.set" in response["result"]["methods"]
    assert "model.options" in response["result"]["methods"]
    assert "toolsets.list" in response["result"]["methods"]
    assert "profile.prepare_runtime" in response["result"]["methods"]
    assert "profile.growth.summary" in response["result"]["methods"]
    assert "profile.learning.graph" in response["result"]["methods"]
    assert "profile.learning.node.detail" in response["result"]["methods"]
    assert "profile.learning.node.edit" in response["result"]["methods"]
    assert "profile.learning.node.delete" in response["result"]["methods"]
    assert "runtime.ensure" in response["result"]["methods"]
    assert "runtime.status" in response["result"]["methods"]
    assert "storage.stats" in response["result"]["methods"]
    assert "approval.respond" in response["result"]["methods"]
    assert "sudo.respond" in response["result"]["methods"]
    assert "secret.respond" in response["result"]["methods"]
    assert "clarify.respond" in response["result"]["methods"]
    assert "run.events" in server._methods
    assert "conversation.render_snapshot" in server._methods
    assert "session.message_metadata.merge" in server._methods
    assert "events.unsubscribe" in server._methods
    assert "team_mission.create" in server._methods
    assert "team_mission.graph" in server._methods
    assert "team_mission.graph.reduce" in server._methods
    assert "team_mission.snapshot.get" in server._methods
    assert "team_mission.result.get" in server._methods
    assert "team_mission.events" in server._methods
    assert "team_capability.snapshot.get" in server._methods
    assert "team_capability.snapshot.refresh" in server._methods
    assert "team_capability.snapshot.bind" in server._methods
    assert "team_mission.team_profile.get" in server._methods
    assert "team_mission.conversation.ensure" in server._methods
    assert "team_mission.conversation.resolve" in server._methods
    assert "team_mission.conversation.render" in server._methods
    assert "team_mission.conversation.list" in server._methods
    assert "team_mission.conversation.participants" in server._methods
    assert "team_mission.conversation.execution_session_ids" in server._methods
    assert "team_mission.conversation.rename" in server._methods
    assert "team_mission.conversation.delete" in server._methods
    assert "team_mission.message.submit" in server._methods
    assert "team_mission.cancel" in server._methods
    assert "team_mission.node.create" in server._methods
    assert "team_mission.edge.create" in server._methods
    assert "team_mission.node.update" in server._methods
    assert "team_mission.node.bind_run" in server._methods
    assert "team_mission.node.history" in server._methods
    assert "team_mission.node.start" in server._methods
    assert "team_mission.plan.complete" in server._methods
    assert "team_mission.plan.approve" in server._methods
    assert "team_mission.schedule.ready" in server._methods
    assert "conversation.memory.propose" in server._methods
    assert "conversation.memory.commit" in server._methods
    assert "conversation.memory.invalidate" in server._methods
    assert "conversation.memory.list" in server._methods
    assert "conversation.activity.context.change" in server._methods
    assert "conversation.context.summary.list" in server._methods
    assert "session.usage" in server._methods
    assert "session.status" in server._methods
    assert "session.branch" in server._methods
    assert server._methods["session.status"].__module__ == "tui_gateway.methods.session"
    assert server._methods["session.branch"].__module__ == "tui_gateway.methods.session_branch"
    assert server._methods["prompt.submit"].__module__ == "tui_gateway.methods.prompt"
    assert server._methods["conversation.render_snapshot"].__module__ == "tui_gateway.methods.conversation_render_snapshot"
    assert server._methods["team_mission.conversation.render"].__module__ == "tui_gateway.methods.conversation_render_snapshot"
    assert server._methods["team_mission.plan.approve"].__module__ == "hermes_team_mission.gateway.runtime_methods"
    assert "model.set" in server._methods
    assert "model.options" in server._methods
    assert "toolsets.list" in server._methods
    assert "profile.prepare_runtime" in server._methods
    assert "profile.growth.summary" in server._methods
    assert "profile.learning.graph" in server._methods
    assert "profile.learning.node.detail" in server._methods
    assert "profile.learning.node.edit" in server._methods
    assert "profile.learning.node.delete" in server._methods
    assert "runtime.ensure" in server._methods
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
    expected_team_mission_owners = {
        "team_mission.create": "hermes_team_mission.gateway.conversation_methods",
        "team_capability.snapshot.get": "hermes_team_mission.gateway.conversation_methods",
        "team_capability.snapshot.refresh": "hermes_team_mission.gateway.conversation_methods",
        "team_capability.snapshot.bind": "hermes_team_mission.gateway.conversation_methods",
        "team_mission.team_profile.get": "hermes_team_mission.gateway.conversation_methods",
        "team_mission.conversation.ensure": "hermes_team_mission.gateway.conversation_methods",
        "team_mission.conversation.resolve": "hermes_team_mission.gateway.conversation_methods",
        "team_mission.conversation.render": "tui_gateway.methods.conversation_render_snapshot",
        "team_mission.conversation.list": "hermes_team_mission.gateway.conversation_methods",
        "team_mission.conversation.participants": "hermes_team_mission.gateway.conversation_methods",
        "team_mission.conversation.execution_session_ids": "hermes_team_mission.gateway.conversation_methods",
        "team_mission.conversation.rename": "hermes_team_mission.gateway.conversation_methods",
        "team_mission.conversation.delete": "hermes_team_mission.gateway.conversation_methods",
        "team_mission.graph": "hermes_team_mission.gateway.runtime_methods",
        "team_mission.graph.reduce": "hermes_team_mission.gateway.runtime_methods",
        "team_mission.snapshot.get": "hermes_team_mission.gateway.snapshot_methods",
        "team_mission.result.get": "hermes_team_mission.gateway.snapshot_methods",
        "team_mission.events": "hermes_team_mission.gateway.runtime_methods",
        "team_mission.message.submit": "hermes_team_mission.gateway.runtime_methods",
        "team_mission.cancel": "hermes_team_mission.gateway.runtime_lifecycle_methods",
        "team_mission.node.create": "hermes_team_mission.gateway.runtime_methods",
        "team_mission.edge.create": "hermes_team_mission.gateway.runtime_methods",
        "team_mission.node.update": "hermes_team_mission.gateway.runtime_methods",
        "team_mission.node.bind_run": "hermes_team_mission.gateway.runtime_lifecycle_methods",
        "team_mission.node.start": "hermes_team_mission.gateway.runtime_lifecycle_methods",
        "team_mission.plan.complete": "hermes_team_mission.gateway.runtime_methods",
        "team_mission.plan.approve": "hermes_team_mission.gateway.runtime_methods",
        "team_mission.schedule.ready": "hermes_team_mission.gateway.runtime_lifecycle_methods",
        "team_mission.node.history": "hermes_team_mission.gateway.history_methods",
        "conversation.memory.propose": "hermes_team_mission.gateway.conversation_memory_methods",
        "conversation.memory.commit": "hermes_team_mission.gateway.conversation_memory_methods",
        "conversation.memory.invalidate": "hermes_team_mission.gateway.conversation_memory_methods",
        "conversation.memory.list": "hermes_team_mission.gateway.conversation_memory_methods",
        "conversation.activity.context.change": "tui_gateway.methods.conversation_activity",
        "conversation.context.summary.list": "hermes_team_mission.gateway.conversation_memory_methods",
    }
    for method_name, owner_module in expected_team_mission_owners.items():
        assert server._methods[method_name].__module__ == owner_module
    assert server._methods["session.resume"].__module__ == "tui_gateway.methods.session"
    assert server._methods["session.branch"].__module__ == "tui_gateway.methods.session_branch"
    assert server._methods["clarify.respond"].__module__ == "tui_gateway.methods.prompt_respond"
    assert server._methods["sudo.respond"].__module__ == "tui_gateway.methods.prompt_respond"
    assert server._methods["secret.respond"].__module__ == "tui_gateway.methods.prompt_respond"
    assert server._methods["approval.respond"].__module__ == "tui_gateway.methods.prompt_respond"
    assert server._methods["approval.policy.get"].__module__ == "tui_gateway.methods.prompt_respond"
    assert server._methods["approval.policy.set"].__module__ == "tui_gateway.methods.prompt_respond"
    assert server._methods["approval.pending.list"].__module__ == "tui_gateway.methods.prompt_respond"
    assert server._methods["config.show"].__module__ == "tui_gateway.methods.integrations"
    assert server._methods["skills.reload"].__module__ == "tui_gateway.methods.integrations"
    assert server._methods["profile.growth.summary"].__module__ == "tui_gateway.methods.profile_registry"
    for method_name in (
        "profile.learning.graph",
        "profile.learning.node.detail",
        "profile.learning.node.edit",
        "profile.learning.node.delete",
    ):
        assert server._methods[method_name].__module__ == "tui_gateway.methods.profile_registry"
    assert {
        getattr(handler, "__module__", "")
        for name, handler in server._methods.items()
        if not name.startswith("_")
    }.isdisjoint({"tui_gateway.server"})


def test_session_message_metadata_merge_json_rpc_persists_transcript_metadata(tmp_path, monkeypatch):
    import importlib

    from hermes_agent.composition.cli_session_store import open_cli_session_store
    from tui_gateway import server

    session_methods = importlib.import_module("tui_gateway.methods.session")
    db = open_cli_session_store(db_path=tmp_path / "state.db")
    try:
        db.sessions.create(session_id="stored-1", source="tui")
        message_id = db.messages.append(
            "stored-1",
            role="assistant",
            content="已创建分身草案",
            metadata={"run_id": "run-1"},
        )
        monkeypatch.setattr(session_methods, "_get_db", lambda: db)

        response = server._methods["session.message_metadata.merge"](
            101,
            {
                "session_id": "stored-1",
                "message_id": str(message_id),
                "role": "assistant",
                "metadata": {
                    "agentProfileDrafts": [
                        {"draftId": "draft-1", "name": "产品经理分身"},
                    ],
                },
            },
        )

        assert response["result"]["message"]["metadata"]["agentProfileDrafts"] == [
            {"draftId": "draft-1", "name": "产品经理分身"},
        ]
        messages = db.messages.all_as_conversation("stored-1", include_storage_metadata=True)
        assert messages[0]["metadata"]["agentProfileDrafts"] == [
            {"draftId": "draft-1", "name": "产品经理分身"},
        ]
    finally:
        db.close()


def test_conversation_render_snapshot_returns_ordinary_render_ready_window(tmp_path, monkeypatch):
    import importlib

    from hermes_agent.composition.cli_session_store import open_cli_session_store
    from tui_gateway import server

    conversation_render_snapshot = importlib.import_module("tui_gateway.methods.conversation_render_snapshot")
    session_methods = importlib.import_module("tui_gateway.methods.session")
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create(session_id="stored-ordinary-1", source="tui")
        db.messages.append(
            "stored-ordinary-1",
            role="assistant",
            content="我会读取文件。",
            participant_id="agent:agent-default",
            metadata={"run_id": "run-1", "turn_id": "turn-1"},
        )
        db.runs.append_event(
            "stored-ordinary-1",
            {
                "type": "tool.complete",
                "session_id": "runtime-1",
                "conversation_session_id": "stored-ordinary-1",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "payload": {
                    "tool_id": "tool-read-1",
                    "name": "read_file",
                    "arguments": {"path": "README.md"},
                    "result_text": "ok",
                },
            },
        )
        db.runs.append_event(
            "stored-ordinary-1",
            {
                "type": "message.delta",
                "session_id": "runtime-1",
                "conversation_session_id": "stored-ordinary-1",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "payload": {
                    "mode": "replace",
                    "text": "正在输出但尚未收到终结帧",
                },
            },
        )
        db.runs.append_event(
            "stored-ordinary-1",
            {
                "type": "artifact.created",
                "session_id": "runtime-1",
                "conversation_session_id": "stored-ordinary-1",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "payload": {
                    "id": "artifact:launch-brief",
                    "title": "launch-brief.md",
                    "path": str(tmp_path / "launch-brief.md"),
                    "mime_type": "text/markdown",
                    "produced_by_run_id": "run-1",
                },
            },
        )
        monkeypatch.setattr(conversation_render_snapshot, "_get_db", lambda: db)
        monkeypatch.setattr(session_methods, "_get_db", lambda: db)

        response = server._methods["conversation.render_snapshot"](
            1,
            {"session_id": "stored-ordinary-1", "limit": 50},
        )

        assert response["result"]["kind"] == "ordinary"
        assert response["result"]["renderReady"] is True
        assert response["result"]["conversation_session_id"] == "stored-ordinary-1"
        assert response["result"]["messages"][0]["text"] == "我会读取文件。"
        run_events = response["result"]["runEvents"]
        assert [event["type"] for event in run_events] == [
            "tool.complete",
            "message.delta",
            "artifact.created",
        ]
        assert run_events[0]["payload"]["tool_id"] == "tool-read-1"
        assert run_events[1]["payload"]["text"] == "正在输出但尚未收到终结帧"
        assert response["result"]["last_event_seq"] == run_events[-1]["seq"]
        assert response["result"]["lastEventSeq"] == response["result"]["last_event_seq"]
        assert response["result"]["projection"]["source"] == "conversation.render_snapshot"

        db.runs.terminate(
            run_id="run-1",
            session_id="stored-ordinary-1",
            target_status="completed",
            cause="worker_emitted",
        )
        completed_response = server._methods["conversation.render_snapshot"](
            2,
            {"session_id": "stored-ordinary-1", "limit": 50},
        )
        assert all(
            event["type"] != "message.delta"
            for event in completed_response["result"]["runEvents"]
        )
        completed_artifacts = [
            event
            for event in completed_response["result"]["runEvents"]
            if event["type"] == "artifact.created"
        ]
        assert len(completed_artifacts) == 1
        assert completed_artifacts[0]["payload"]["id"] == "artifact:launch-brief"
        assert completed_response["result"]["last_event_seq"] > response["result"]["last_event_seq"]
    finally:
        db.close()


def test_conversation_render_snapshot_returns_completed_team_projection_without_runtime_replay_events(tmp_path, monkeypatch):
    import importlib

    from hermes_agent.composition.cli_session_store import open_cli_session_store
    from tui_gateway import server

    conversation_render_snapshot = importlib.import_module("tui_gateway.methods.conversation_render_snapshot")
    session_methods = importlib.import_module("tui_gateway.methods.session")
    team_mission = team_mission_gateway()
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create(session_id="team-session-1", source="team_mission")
        db.messages.append(
            "team-session-1",
            role="assistant",
            content="团队任务完成。",
            participant_id="leader:conversation-1",
            metadata={"run_id": "team-run-1", "turn_id": "team-turn-1"},
        )
        db.runs.append_event(
            "team-session-1",
            {
                "type": "tool.complete",
                "seq": 3,
                "session_id": "runtime-team-1",
                "conversation_session_id": "team-session-1",
                "run_id": "team-run-1",
                "turn_id": "team-turn-1",
                "runtime_scope_key": "team:conversation-1:leader",
                "payload": {
                    "tool_id": "team-tool-1",
                    "name": "team_mission_start_task",
                    "result_text": "accepted",
                },
            },
        )
        db.upsert_team_mission_conversation(
            conversation_id="conversation-1",
            conversation_session_id="team-session-1",
            team_id="team-1",
            title="团队会话",
        )
        monkeypatch.setattr(conversation_render_snapshot, "_get_db", lambda: db)
        monkeypatch.setattr(session_methods, "_get_db", lambda: db)
        monkeypatch.setattr(team_mission, "_get_db", lambda: db)

        response = server._methods["conversation.render_snapshot"](
            1,
            {
                "kind": "team_mission",
                "conversation_id": "conversation-1",
                "runtime_scope_key": "team:conversation-1:leader",
                "includeToolEvents": True,
            },
        )

        assert response["result"]["kind"] == "team_mission"
        assert response["result"]["renderReady"] is True
        conversation = response["result"]["conversation"]
        assert conversation["conversation_session_id"] == "team-session-1"
        assert "conversation_id" not in conversation
        assert response["result"]["conversation_session_id"] == "team-session-1"
        assert response["result"]["graph"]["recent_messages"][0]["text"] == "团队任务完成。"
        assert response["result"]["messages"][0]["text"] == "团队任务完成。"
        assert response["result"]["runEvents"] == []
        assert response["result"]["toolEvents"] == []
        assert response["result"]["last_event_seq"] > 0
    finally:
        db.close()


def test_team_mission_conversation_render_returns_room_snapshot(tmp_path, monkeypatch):
    import importlib

    from hermes_agent.composition.cli_session_store import open_cli_session_store
    from tui_gateway import server

    conversation_render_snapshot = importlib.import_module("tui_gateway.methods.conversation_render_snapshot")
    session_methods = importlib.import_module("tui_gateway.methods.session")
    team_mission = team_mission_gateway()
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create(session_id="team-session-1", source="team_mission")
        db.messages.append(
            "team-session-1",
            role="assistant",
            content="团队房间首屏消息。",
            participant_id="leader:conversation-1",
            metadata={"run_id": "team-run-1", "turn_id": "team-turn-1"},
        )
        db.runs.append_event(
            "team-session-1",
            {
                "type": "tool.complete",
                "seq": 5,
                "session_id": "runtime-team-1",
                "conversation_session_id": "team-session-1",
                "run_id": "team-run-1",
                "turn_id": "team-turn-1",
                "payload": {
                    "tool_id": "team-tool-1",
                    "name": "team_mission_start_task",
                    "result_text": "accepted",
                },
            },
        )
        db.upsert_team_mission_conversation(
            conversation_id="conversation-1",
            conversation_session_id="team-session-1",
            team_id="team-1",
            title="团队会话",
            active_mission_id="mission-1",
        )
        db.upsert_team_mission(
            mission_id="mission-1",
            conversation_id="conversation-1",
            team_id="team-1",
            title="团队任务",
            objective="测试",
            status="completed",
            metadata={"conversationTeamSessionId": "team-session-1"},
        )
        db.upsert_team_mission_result(
            mission_id="mission-1",
            activity_id="mission:mission-1",
            status="completed",
            outcome="completed",
            summary_text="最终结论：PASS",
            node_results=[],
            artifact_refs=[],
        )
        monkeypatch.setattr(conversation_render_snapshot, "_get_db", lambda: db)
        monkeypatch.setattr(session_methods, "_get_db", lambda: db)
        monkeypatch.setattr(team_mission, "_get_db", lambda: db)

        response = server._methods["team_mission.conversation.render"](
            1,
            {
                "conversation_id": "conversation-1",
                "limit": 100,
                "includeRunEvents": True,
                "includeToolEvents": True,
                "toolEventsLimit": 50,
            },
        )

        assert response["result"]["kind"] == "team_mission"
        assert response["result"]["renderReady"] is True
        assert response["result"]["projection"]["source"] == "team_mission.conversation.render"
        assert response["result"]["conversation_session_id"] == "team-session-1"
        conversation = response["result"]["conversation"]
        assert conversation["conversation_session_id"] == "team-session-1"
        assert "conversation_id" not in conversation
        assert response["result"]["mission"]["mission_id"] == "mission-1"
        assert response["result"]["projection"]["leaderReportStatus"] == "pending"
        assert response["result"]["projection"]["leaderReportRunId"] == ""
        assert response["result"]["projection"]["activeResult"]["status"] == "completed"
        assert response["result"]["messages"][0]["text"] == "团队房间首屏消息。"
        assert response["result"]["runEvents"] == []
        assert response["result"]["toolEvents"] == []
        watermarks = response["result"]["projection"]["activityWatermarks"]
        assert {item["activity_id"] for item in watermarks} >= {
            "chat:team-session-1",
            "mission:mission-1",
        }
        mission_watermark = next(
            item for item in watermarks if item["activity_id"] == "mission:mission-1"
        )
        assert mission_watermark["terminal"] is True
        assert mission_watermark["replay_policy"] == "cursor_only"
    finally:
        db.close()


def test_conversation_render_snapshot_returns_active_team_structural_runtime_events(tmp_path, monkeypatch):
    import importlib

    from hermes_agent.composition.cli_session_store import open_cli_session_store
    from tui_gateway import server

    conversation_render_snapshot = importlib.import_module("tui_gateway.methods.conversation_render_snapshot")
    session_methods = importlib.import_module("tui_gateway.methods.session")
    team_mission = team_mission_gateway()
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create(session_id="team-session-1", source="team_mission")
        db.messages.append(
            "team-session-1",
            role="assistant",
            content="团队任务进行中。",
            participant_id="leader:conversation-1",
            metadata={"run_id": "completed-run-1", "turn_id": "completed-turn-1"},
        )
        db.runs.upsert(
            run_id="active-team-run-1",
            session_id="team-session-1",
            runtime_scope_key="team:conversation-1:leader",
            turn_id="active-team-turn-1",
            execution_session_id="runtime-team-active",
            status="running",
        )
        db.runs.append_event(
            "team-session-1",
            {
                "type": "message.delta",
                "session_id": "runtime-team-old",
                "conversation_session_id": "team-session-1",
                "run_id": "completed-run-1",
                "turn_id": "completed-turn-1",
                "runtime_scope_key": "team:conversation-1:leader",
                "payload": {"text": "old"},
            },
        )
        db.runs.append_event(
            "team-session-1",
            {
                "type": "message.delta",
                "session_id": "runtime-team-active",
                "conversation_session_id": "team-session-1",
                "run_id": "active-team-run-1",
                "turn_id": "active-team-turn-1",
                "runtime_scope_key": "team:conversation-1:leader",
                "payload": {"text": "active"},
            },
        )
        db.runs.append_event(
            "team-session-1",
            {
                "type": "tool.start",
                "session_id": "runtime-team-active",
                "conversation_session_id": "team-session-1",
                "run_id": "active-team-run-1",
                "turn_id": "active-team-turn-1",
                "runtime_scope_key": "team:conversation-1:leader",
                "payload": {"tool_id": "tool-1", "name": "web_search"},
            },
        )
        db.upsert_team_mission_conversation(
            conversation_id="conversation-1",
            conversation_session_id="team-session-1",
            team_id="team-1",
            title="团队会话",
            active_mission_id="mission-active",
        )
        monkeypatch.setattr(conversation_render_snapshot, "_get_db", lambda: db)
        monkeypatch.setattr(session_methods, "_get_db", lambda: db)
        monkeypatch.setattr(team_mission, "_get_db", lambda: db)

        response = server._methods["conversation.render_snapshot"](
            1,
            {
                "kind": "team_mission",
                "conversation_id": "conversation-1",
                "runtime_scope_key": "team:conversation-1:leader",
            },
        )

        assert response["result"]["kind"] == "team_mission"
        assert response["result"]["conversation"]["running"] is True
        assert response["result"]["conversation"]["active_run_id"] == "active-team-run-1"
        assert [event["run_id"] for event in response["result"]["runEvents"]] == [
            "active-team-run-1",
            "active-team-run-1",
        ]
        assert [event["type"] for event in response["result"]["runEvents"]] == [
            "message.delta",
            "tool.start",
        ]
        assert response["result"]["runEvents"][1]["payload"]["tool_id"] == "tool-1"
    finally:
        db.close()


def test_conversation_render_snapshot_cap_drops_oversized_single_items():
    import importlib

    conversation_render_snapshot = importlib.import_module("tui_gateway.methods.conversation_render_snapshot")
    result = {
        "kind": "ordinary",
        "schemaVersion": "test",
        "renderReady": True,
        "messages": [{"id": "huge-message", "text": "m" * 20_000}],
        "runEvents": [{"type": "tool.complete", "payload": {"result": "r" * 20_000}}],
        "pageInfo": {},
        "projection": {"source": "conversation.render_snapshot"},
    }

    capped = conversation_render_snapshot._cap_render_result(result, max_bytes=2_048)  # noqa: SLF001

    assert len(json.dumps(capped, ensure_ascii=False).encode("utf-8")) <= 2_048
    assert capped["transportTruncated"] is True
    assert capped["pageInfo"]["hasMore"] is True
    assert capped["projection"]["transportTruncated"] is True
    assert capped["messages"] == []
    assert capped["runEvents"] == []


def test_conversation_render_snapshot_preserves_team_assistant_run_ids(tmp_path, monkeypatch):
    import importlib

    from hermes_agent.composition.cli_session_store import open_cli_session_store
    from tui_gateway import server

    conversation_render_snapshot = importlib.import_module("tui_gateway.methods.conversation_render_snapshot")
    session_methods = importlib.import_module("tui_gateway.methods.session")
    team_mission = team_mission_gateway()
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create(session_id="team-session-1", source="team_mission")
        shared_run_id = "team-mission:mission-1:conversation:run-synthesis"
        db.messages.append(
            "team-session-1",
            role="assistant",
            content="第一轮汇总。",
            participant_id="leader:conversation-1",
            metadata={
                "run_id": shared_run_id,
                "team_mission": {
                    "mission_id": "mission-1",
                    "source_run_id": "run-synthesis",
                    "source_seq": "101",
                },
            },
        )
        db.messages.append(
            "team-session-1",
            role="user",
            content="继续。",
            participant_id="user",
        )
        db.messages.append(
            "team-session-1",
            role="assistant",
            content="第二轮汇总。",
            participant_id="leader:conversation-1",
            metadata={
                "run_id": shared_run_id,
                "team_mission": {
                    "mission_id": "mission-1",
                    "source_run_id": "run-synthesis",
                    "source_seq": "202",
                },
            },
        )
        db.upsert_team_mission_conversation(
            conversation_id="conversation-1",
            conversation_session_id="team-session-1",
            team_id="team-1",
            title="团队会话",
            active_mission_id="mission-1",
        )
        db.upsert_team_mission(
            mission_id="mission-1",
            conversation_id="conversation-1",
            team_id="team-1",
            title="团队任务",
            objective="测试",
            status="completed",
            metadata={"conversationTeamSessionId": "team-session-1"},
        )
        monkeypatch.setattr(conversation_render_snapshot, "_get_db", lambda: db)
        monkeypatch.setattr(session_methods, "_get_db", lambda: db)
        monkeypatch.setattr(team_mission, "_get_db", lambda: db)

        response = server._methods["conversation.render_snapshot"](
            1,
            {
                "kind": "team_mission",
                "conversation_id": "conversation-1",
            },
        )

        messages = response["result"]["messages"]
        assistant_run_ids = [
            message["metadata"]["run_id"]
            for message in messages
            if message["role"] == "assistant"
        ]
        assert len(assistant_run_ids) == 2
        assert assistant_run_ids == [shared_run_id, shared_run_id]
        assert "original_run_id" not in messages[0]["metadata"]
        assert messages[0]["metadata"]["team_mission"]["source_run_id"] == "run-synthesis"
        assert messages[2]["metadata"]["team_mission"]["source_seq"] == "202"
        assert response["result"]["runEvents"] == []
    finally:
        db.close()


def test_conversation_render_snapshot_filters_node_transcript_but_keeps_mission_summary(tmp_path, monkeypatch):
    import importlib

    from hermes_agent.composition.cli_session_store import open_cli_session_store
    from tui_gateway import server

    conversation_render_snapshot = importlib.import_module("tui_gateway.methods.conversation_render_snapshot")
    session_methods = importlib.import_module("tui_gateway.methods.session")
    team_mission = team_mission_gateway()
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create(session_id="team-session-1", source="team_mission")
        db.messages.append(
            "team-session-1",
            role="user",
            content="开始团队任务。",
            participant_id="user",
            metadata={"transcript_activity_kind": "mission_start"},
        )
        db.messages.append(
            "team-session-1",
            role="assistant",
            content="节点内部细节。",
            participant_id="member:worker",
            metadata={"transcript_activity_kind": "mission_node"},
        )
        db.messages.append(
            "team-session-1",
            role="assistant",
            content="最终汇总。",
            participant_id="leader:conversation-1",
            metadata={
                "transcript_activity_kind": "mission_summary",
                "team_mission": {
                    "kind": "mission_summary",
                    "mission_id": "mission-1",
                },
            },
        )
        db.upsert_team_mission_conversation(
            conversation_id="conversation-1",
            conversation_session_id="team-session-1",
            team_id="team-1",
            title="团队会话",
            active_mission_id="mission-1",
        )
        db.upsert_team_mission(
            mission_id="mission-1",
            conversation_id="conversation-1",
            team_id="team-1",
            title="团队任务",
            objective="测试",
            status="completed",
            metadata={"conversationTeamSessionId": "team-session-1"},
        )
        monkeypatch.setattr(conversation_render_snapshot, "_get_db", lambda: db)
        monkeypatch.setattr(session_methods, "_get_db", lambda: db)
        monkeypatch.setattr(team_mission, "_get_db", lambda: db)

        response = server._methods["conversation.render_snapshot"](
            1,
            {
                "kind": "team_mission",
                "conversation_id": "conversation-1",
                "includeRunEvents": True,
            },
        )

        assert [message["text"] for message in response["result"]["messages"]] == [
            "开始团队任务。",
            "最终汇总。",
        ]
        assert [message["text"] for message in response["result"]["graph"]["recent_messages"]] == [
            "开始团队任务。",
            "最终汇总。",
        ]
        assert response["result"]["runEvents"] == []
        assert response["result"]["toolEvents"] == []
    finally:
        db.close()


def test_conversation_render_snapshot_normalizes_same_turn_team_assistant_tool_messages(tmp_path, monkeypatch):
    import importlib

    from hermes_agent.composition.cli_session_store import open_cli_session_store
    from tui_gateway import server

    conversation_render_snapshot = importlib.import_module("tui_gateway.methods.conversation_render_snapshot")
    session_methods = importlib.import_module("tui_gateway.methods.session")
    team_mission = team_mission_gateway()
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create(session_id="team-session-1", source="team_mission")
        shared_run_id = "team-leader-run-1"
        shared_turn_id = "team-leader-turn-1"
        db.messages.append(
            "team-session-1",
            role="user",
            content="创建团队任务。",
            participant_id="user",
            metadata={"run_id": shared_run_id, "turn_id": shared_turn_id},
        )
        db.messages.append(
            "team-session-1",
            role="assistant",
            content="好的，开始创建。",
            participant_id="leader:conversation-1",
            metadata={"run_id": shared_run_id, "turn_id": shared_turn_id},
        )
        db.messages.append(
            "team-session-1",
            role="tool",
            content='{"success": true}',
            participant_id="leader:conversation-1",
            metadata={"run_id": shared_run_id, "turn_id": shared_turn_id},
        )
        db.messages.append(
            "team-session-1",
            role="assistant",
            content="团队任务已接受。",
            participant_id="leader:conversation-1",
            metadata={"run_id": shared_run_id, "turn_id": shared_turn_id},
        )
        db.upsert_team_mission_conversation(
            conversation_id="conversation-1",
            conversation_session_id="team-session-1",
            team_id="team-1",
            title="团队会话",
            active_mission_id="mission-1",
        )
        db.upsert_team_mission(
            mission_id="mission-1",
            conversation_id="conversation-1",
            team_id="team-1",
            title="团队任务",
            objective="测试",
            status="completed",
            metadata={"conversationTeamSessionId": "team-session-1"},
        )
        monkeypatch.setattr(conversation_render_snapshot, "_get_db", lambda: db)
        monkeypatch.setattr(session_methods, "_get_db", lambda: db)
        monkeypatch.setattr(team_mission, "_get_db", lambda: db)

        response = server._methods["conversation.render_snapshot"](
            1,
            {
                "kind": "team_mission",
                "conversation_id": "conversation-1",
            },
        )

        messages = response["result"]["messages"]
        assistant_run_ids = [
            message["metadata"]["run_id"]
            for message in messages
            if message["role"] == "assistant"
        ]
        assert len(assistant_run_ids) == 2
        assert assistant_run_ids == [shared_run_id, shared_run_id]
        assert [message["role"] for message in messages] == ["user", "assistant", "tool", "assistant"]
        assert "original_run_id" not in messages[1]["metadata"]
        assert "original_run_id" not in messages[3]["metadata"]
    finally:
        db.close()


def test_model_set_gateway_method_uses_stable_params(monkeypatch):
    from types import SimpleNamespace

    from tui_gateway import server

    agent = SimpleNamespace(reasoning_config=None)
    session = {"running": False, "agent": agent}
    monkeypatch.setitem(server._sessions, "runtime-1", session)
    calls = []

    def fake_apply_model_switch(session_id, active_session, model, **kwargs):
        calls.append((session_id, active_session, model, kwargs))
        return {"value": model, "warning": ""}

    monkeypatch.setattr(server, "_apply_model_switch", fake_apply_model_switch)
    response = server._methods["model.set"](
        1,
        {
            "model": "gpt-5",
            "session_id": "runtime-1",
            "model_descriptor": {
                "id": "gpt-5",
                "catalog_source": "dovie_model_registry",
                "provider": "openai",
                "reasoning_enabled": True,
                "reasoning_efforts": ["low", "medium", "high"],
                "reasoning_effort": "high",
            },
        },
    )

    assert response["result"] == {"key": "model", "value": "gpt-5", "warning": ""}
    assert calls == [(
        "runtime-1",
        session,
        "gpt-5",
        {
            "parsed_flags": ("gpt-5", "", False, False, True),
            "catalog_model_id": "gpt-5",
        },
    )]
    assert session["model_descriptor"] == {
        "id": "gpt-5",
        "catalog_source": "dovie_model_registry",
        "provider": "openai",
        "reasoning_enabled": True,
        "reasoning_efforts": ["low", "medium", "high"],
        "reasoning_effort": "high",
    }
    assert agent.model_descriptor == session["model_descriptor"]
    assert agent.reasoning_config == {"enabled": True, "effort": "high"}
    assert agent.request_overrides["reasoning_effort"] == "high"


def test_model_set_rejects_mismatched_catalog_descriptor(monkeypatch):
    from types import SimpleNamespace

    from tui_gateway import server

    session = {"running": False, "agent": SimpleNamespace()}
    monkeypatch.setitem(server._sessions, "runtime-mismatch", session)

    response = server._methods["model.set"](
        1,
        {
            "model": "gpt-5",
            "session_id": "runtime-mismatch",
            "model_descriptor": {
                "id": "glm-5.2",
                "catalog_source": "dovie_model_registry",
            },
        },
    )

    assert response["error"]["code"] == 4002
    assert "descriptor id" in response["error"]["message"]


def test_model_set_same_session_model_is_network_free(monkeypatch):
    from types import SimpleNamespace

    from tui_gateway import server

    agent = SimpleNamespace(
        model="deepseek-v4-pro",
        provider="custom",
        base_url="https://api.doviemate.com/api/v1/llm-proxy/v1",
        api_key="runtime-token",
        api_mode="chat_completions",
        reasoning_config=None,
    )
    session = {"running": False, "agent": agent}
    monkeypatch.setitem(server._sessions, "runtime-same-model", session)

    def unexpected_switch(**_kwargs):
        raise AssertionError("same-session model reapply must stay local")

    monkeypatch.setattr("hermes_cli.model_switch.switch_model", unexpected_switch)

    response = server._methods["model.set"](
        1,
        {
            "model": "deepseek-v4-pro",
            "session_id": "runtime-same-model",
            "model_descriptor": {
                "id": "deepseek-v4-pro",
                "catalog_source": "dovie_model_registry",
                "provider": "Dovie Cloud",
            },
        },
    )

    assert response["result"] == {
        "key": "model",
        "value": "deepseek-v4-pro",
        "warning": "",
    }
    assert session["model_override"] == {
        "model": "deepseek-v4-pro",
        "provider": "custom",
        "base_url": "https://api.doviemate.com/api/v1/llm-proxy/v1",
        "api_mode": "chat_completions",
    }


def test_worker_prompt_uses_preselected_control_plane_model_without_switch_probe(
    monkeypatch,
):
    from tui_gateway import process_role, server
    from tui_gateway.methods import prompt as prompt_methods

    session = {
        "agent": None,
        "model_override": {
            "model": "glm-5.2",
            "model_explicit": True,
        },
    }
    switch_calls = []

    def unexpected_switch(*args, **kwargs):
        switch_calls.append((args, kwargs))
        raise AssertionError("worker bootstrap must not enter remote model validation")

    monkeypatch.setattr(process_role, "IS_WORKER_PROCESS", True)
    monkeypatch.setattr(server, "_apply_model_switch", unexpected_switch)

    prompt_methods._apply_prompt_model_selection(
        "runtime-worker",
        session,
        "glm-5.2",
        {
            "id": "glm-5.2",
            "reasoning_enabled": True,
            "reasoning_effort": "high",
        },
    )

    assert switch_calls == []
    assert session["model_override"] == {
        "model": "glm-5.2",
        "model_explicit": True,
    }
    assert session["model_descriptor"] == {
        "id": "glm-5.2",
        "reasoning_enabled": True,
        "reasoning_effort": "high",
    }


def test_worker_prompt_does_not_probe_models_when_optional_descriptor_is_absent(
    monkeypatch,
):
    """Internal worker routing already selected the model in RunStartFrame."""

    from tui_gateway import process_role, server
    from tui_gateway.methods import prompt as prompt_methods

    session = {
        "agent": None,
        "model_override": {
            "model": "glm-5.2",
            "model_explicit": True,
        },
    }

    def unexpected_switch(*_args, **_kwargs):
        raise AssertionError("worker bootstrap must not probe relay /models")

    monkeypatch.setattr(process_role, "IS_WORKER_PROCESS", True)
    monkeypatch.setattr(server, "_apply_model_switch", unexpected_switch)

    prompt_methods._apply_prompt_model_selection(
        "runtime-worker",
        session,
        "glm-5.2",
        {},
    )

    assert session["model_override"] == {
        "model": "glm-5.2",
        "model_explicit": True,
    }
    assert "model_descriptor" not in session


def test_direct_prompt_still_uses_canonical_model_switch(monkeypatch):
    from tui_gateway import process_role, server
    from tui_gateway.methods import prompt as prompt_methods

    session = {
        "agent": None,
        "model_override": {
            "model": "glm-5.2",
            "model_explicit": True,
        },
    }
    switch_calls = []
    monkeypatch.setattr(process_role, "IS_WORKER_PROCESS", False)
    monkeypatch.setattr(
        server,
        "_apply_model_switch",
        lambda sid, active_session, model, **kwargs: switch_calls.append(
            (sid, active_session, model, kwargs)
        ),
    )

    prompt_methods._apply_prompt_model_selection(
        "runtime-direct",
        session,
        "glm-5.2",
        {"id": "glm-5.2"},
    )

    assert switch_calls == [(
        "runtime-direct",
        session,
        "glm-5.2",
        {"parsed_flags": ("glm-5.2", "", False, False, True)},
    )]
    assert session["model_descriptor"] == {"id": "glm-5.2"}


def test_prompt_model_selection_is_session_scoped(monkeypatch):
    from types import SimpleNamespace

    from tui_gateway.methods import prompt as prompt_methods

    session = {"running": False, "agent": SimpleNamespace()}
    calls = []
    monkeypatch.setattr(
        prompt_methods,
        "_apply_model_switch",
        lambda sid, active_session, model, **kwargs: calls.append(
            (sid, active_session, model, kwargs)
        ),
    )

    prompt_methods._apply_prompt_model_selection(
        "runtime-prompt",
        session,
        "gpt-5",
        {"id": "gpt-5", "catalog_source": "dovie_model_registry"},
    )

    assert calls == [(
        "runtime-prompt",
        session,
        "gpt-5",
        {
            "parsed_flags": ("gpt-5", "", False, False, True),
            "catalog_model_id": "gpt-5",
        },
    )]
    assert session["model_descriptor"] == {
        "id": "gpt-5",
        "catalog_source": "dovie_model_registry",
    }


def test_worker_prompt_failure_defers_terminal_transition_to_main(monkeypatch):
    from tui_gateway import process_role, server
    from tui_gateway.methods import prompt as prompt_methods

    db_lookups = []
    monkeypatch.setattr(process_role, "IS_WORKER_PROCESS", True)
    monkeypatch.setattr(
        server,
        "_db_for_stable_session",
        lambda session_id: db_lookups.append(session_id),
    )

    prompt_methods._mark_prompt_run_failed(
        run_id="run-worker-failure",
        conversation_session_id="conversation-worker-failure",
        runtime_scope_key="profile:test",
        turn_id="turn-worker-failure",
        message="preflight failed",
    )

    assert db_lookups == []


def test_pending_model_descriptor_is_replayed_when_agent_is_bound():
    from types import SimpleNamespace

    from tui_gateway.services.model_descriptor import (
        bind_session_agent,
        set_session_model_descriptor,
    )

    session = {"agent": None}
    set_session_model_descriptor(
        session,
        {
            "id": "gpt-5.6-luna",
            "reasoning_enabled": True,
            "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
            "reasoning_effort": "xhigh",
            "request_params": {"reasoning_effort": "medium"},
        },
    )
    agent = SimpleNamespace(
        reasoning_config=None,
        request_overrides={"service_tier": "priority"},
    )

    bind_session_agent(session, agent)

    assert session["agent"] is agent
    assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}
    assert agent.request_overrides == {
        "service_tier": "priority",
        "reasoning_effort": "xhigh",
    }


def test_clearing_model_descriptor_restores_shadowed_request_override():
    from types import SimpleNamespace

    from tui_gateway.services.model_descriptor import set_session_model_descriptor

    agent = SimpleNamespace(
        reasoning_config=None,
        request_overrides={"reasoning_effort": "low", "service_tier": "priority"},
    )
    session = {"agent": agent}
    set_session_model_descriptor(
        session,
        {
            "id": "gpt-5.6-luna",
            "reasoning_enabled": True,
            "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"],
            "reasoning_effort": "xhigh",
            "request_params": {"reasoning_effort": "medium"},
        },
    )
    assert agent.request_overrides["reasoning_effort"] == "xhigh"

    set_session_model_descriptor(session, {}, clear_if_empty=True)

    assert agent.request_overrides == {
        "reasoning_effort": "low",
        "service_tier": "priority",
    }


def test_model_descriptor_selects_responses_transport_and_restores_chat_mode():
    from types import SimpleNamespace

    from tui_gateway.services.model_descriptor import set_session_model_descriptor

    compressor = SimpleNamespace(api_mode="chat_completions")
    agent = SimpleNamespace(
        api_mode="chat_completions",
        reasoning_config=None,
        request_overrides={},
        context_compressor=compressor,
        _transport_cache={"chat_completions": object()},
        _primary_runtime={"api_mode": "chat_completions"},
    )
    session = {"agent": agent}

    set_session_model_descriptor(
        session,
        {
            "id": "gpt-responses",
            "api_format": "openai_responses",
        },
    )

    assert agent.api_mode == "codex_responses"
    assert agent.context_compressor.api_mode == "codex_responses"
    assert agent._primary_runtime["api_mode"] == "codex_responses"
    assert agent._transport_cache == {}

    set_session_model_descriptor(session, {}, clear_if_empty=True)

    assert agent.api_mode == "chat_completions"
    assert agent.context_compressor.api_mode == "chat_completions"
    assert agent._primary_runtime["api_mode"] == "chat_completions"


def test_model_descriptor_can_downgrade_responses_model_to_chat_compatibility():
    from types import SimpleNamespace

    from tui_gateway.services.model_descriptor import set_session_model_descriptor

    agent = SimpleNamespace(
        api_mode="chat_completions",
        reasoning_config=None,
        request_overrides={},
        _transport_cache={},
        _primary_runtime={"api_mode": "chat_completions"},
    )
    session = {"agent": agent}

    set_session_model_descriptor(
        session,
        {"id": "gpt-new", "api_format": "codex_responses"},
    )
    set_session_model_descriptor(
        session,
        {"id": "gpt-legacy", "api_format": "openai"},
    )

    assert agent.api_mode == "chat_completions"
    assert agent._primary_runtime["api_mode"] == "chat_completions"


def test_model_descriptor_never_replaces_codex_executor_transport():
    from types import SimpleNamespace

    from tui_gateway.services.model_descriptor import set_session_model_descriptor

    agent = SimpleNamespace(
        api_mode="codex_app_server",
        reasoning_config=None,
        request_overrides={},
    )
    session = {"agent": agent}

    set_session_model_descriptor(
        session,
        {"id": "gpt-codex", "api_format": "openai_responses"},
    )

    assert agent.api_mode == "codex_app_server"


def test_model_set_clears_previous_reasoning_override_for_model_default(monkeypatch):
    from types import SimpleNamespace

    from tui_gateway import server

    agent = SimpleNamespace(reasoning_config={"enabled": True, "effort": "max"})
    session = {"running": False, "agent": agent}
    monkeypatch.setitem(server._sessions, "runtime-default", session)
    monkeypatch.setattr(
        server,
        "_apply_model_switch",
        lambda _session_id, _session, model, **_kwargs: {"value": model, "warning": ""},
    )

    response = server._methods["model.set"](
        1,
        {
            "model": "custom-reasoner",
            "session_id": "runtime-default",
            "model_descriptor": {
                "id": "custom-reasoner",
                "reasoning_enabled": True,
            },
        },
    )

    assert "error" not in response
    assert agent.reasoning_config is None


def test_model_set_applies_binary_reasoning_selection(monkeypatch):
    from types import SimpleNamespace

    from tui_gateway import server

    agent = SimpleNamespace(reasoning_config=None)
    session = {"running": False, "agent": agent}
    monkeypatch.setitem(server._sessions, "runtime-binary", session)
    monkeypatch.setattr(
        server,
        "_apply_model_switch",
        lambda _session_id, _session, model, **_kwargs: {"value": model, "warning": ""},
    )

    response = server._methods["model.set"](
        1,
        {
            "model": "qwen3.6-plus",
            "session_id": "runtime-binary",
            "model_descriptor": {
                "id": "qwen3.6-plus",
                "reasoning_enabled": True,
                "reasoning_efforts": ["none", "enabled"],
                "reasoning_effort": "enabled",
            },
        },
    )

    assert "error" not in response
    assert agent.reasoning_config == {"enabled": True}


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
    assert response["result"]["runtime_scope_key"] == "profile:agent-a"
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


def test_runtime_ensure_never_reports_false_readiness_outside_async_dispatch():
    """The registry fallback must not resurrect the old fake-ready ABI."""
    from tui_gateway import server

    response = server._methods["runtime.ensure"](
        1,
        {
            "agentProfileId": "agent-a",
            "agentProfileVersionId": "version-1",
            "dovie_profile": {
                "id": "agent-a",
                "hermesHomePath": "/tmp/hermes-agent-a",
                "runtimeScopeKey": "profile:agent-a:version:version-1",
            },
        },
    )

    assert response["error"]["code"] == 5024
    assert response["error"]["data"]["ready"] is False
    assert response["error"]["data"]["runtime_scope_key"] == "profile:agent-a"


def test_runtime_status_returns_lightweight_diagnostics(monkeypatch):
    from tui_gateway import server

    monkeypatch.setattr(
        "channels.runtime_status.read_runtime_status",
        lambda: {"gateway_state": "running", "pid": 1234},
    )
    response = server._methods["runtime.status"](1, {})

    assert response["result"]["status"] == "running"
    assert response["result"]["available"] is True
    assert response["result"]["runtime"]["pid"] == 1234


def test_session_db_persists_run_registry_and_event_log(tmp_path):
    from hermes_agent.composition.cli_session_store import open_cli_session_store

    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("session-1", "tui", transient=True)

        reservation = db.runs.reserve_if_idle(
            run_id="run-1",
            session_id="session-1",
            runtime_scope_key="profile:agent-default",
            turn_id="turn-1",
            execution_session_id="runtime-1",
            status="queued",
            metadata={"source": "test"},
        )
        assert reservation["created"] is True
        assert reservation["conflict"] is None
        assert reservation["run"]["runtime_scope_key"] == "profile:agent-default"

        conflict = db.runs.reserve_if_idle(
            run_id="run-2",
            session_id="session-1",
            runtime_scope_key="profile:agent-default",
            turn_id="turn-2",
            execution_session_id="runtime-1",
            status="queued",
        )
        assert conflict["created"] is False
        assert conflict["conflict"]["run_id"] == "run-1"

        event = db.runs.append_event(
            "session-1",
            {
                "type": "message.delta",
                "session_id": "runtime-1",
                "conversation_session_id": "session-1",
                "run_id": "run-1",
                "turn_id": "turn-1",
                "runtime_scope_key": "profile:agent-default",
                "owner_metadata": {
                    "gateway_pid": 4321,
                    "gateway_instance_id": "runtime-owner",
                },
                "seq": 1,
                "payload": {"text": "hello"},
            },
        )
        assert event["seq"] == 1

        events = db.runs.list_events(
            "session-1",
            runtime_scope_key="profile:agent-default",
        )
        assert [item["seq"] for item in events] == [1]

        run = db.runs.get("run-1")
        assert run["last_seq"] == 1
        assert run["metadata"]["source"] == "test"
        assert run["metadata"]["gateway_pid"] == 4321
        assert run["metadata"]["gateway_instance_id"] == "runtime-owner"

        rows = db._conn.execute("SELECT transient FROM sessions WHERE id = ?", ("session-1",)).fetchone()
        assert rows["transient"] == 1
    finally:
        db.close()


def test_session_db_keeps_terminal_run_closed_after_late_delta(tmp_path):
    from hermes_agent.composition.cli_session_store import open_cli_session_store

    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("session-1", "tui")
        first = db.runs.reserve_if_idle(
            run_id="run-1",
            session_id="session-1",
            runtime_scope_key="profile:agent-default",
            turn_id="turn-1",
            execution_session_id="runtime-1",
            status="running",
        )
        assert first["created"] is True

        db.runs.append_event(
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
        assert db.runs.get("run-1")["status"] == "completed"
        terminal_seq = db.runs.list_events("session-1")[-1]["seq"]

        db.runs.append_event(
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

        terminal = db.runs.get("run-1")
        assert terminal["status"] == "completed"
        assert terminal["last_seq"] == terminal_seq
        assert terminal["completed_at"] is not None
        events = db.runs.list_events("session-1", include_internal=True)
        assert [event["seq"] for event in events] == [terminal_seq]
        assert [event["type"] for event in events] == ["message.complete"]
        assert db.runs.list_events(
            "session-1",
            active_only=True,
            runtime_scope_key="profile:agent-default",
        ) == []

        second = db.runs.reserve_if_idle(
            run_id="run-2",
            session_id="session-1",
            runtime_scope_key="profile:agent-default",
            turn_id="turn-2",
            execution_session_id="runtime-2",
            status="queued",
        )
        assert second["created"] is True
        assert second["conflict"] is None
    finally:
        db.close()


def test_run_control_session_status_recovers_dead_gateway_active_run(tmp_path, caplog):
    from hermes_agent.composition.cli_session_store import open_cli_session_store
    from tui_gateway.services import run_control

    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("team-session-recovered", "tui")
        stale_time = time.time() - 301
        db.runs.upsert(
            run_id="stale-run",
            session_id="team-session-recovered",
            runtime_scope_key="team:conversation:leader",
            execution_session_id="runtime-stale",
            status="running",
            started_at=stale_time,
            updated_at=stale_time,
            metadata={
                "gateway_pid": os.getpid(),
                "gateway_instance_id": "previous-gateway",
            },
        )

        with caplog.at_level(logging.WARNING, logger="tui_gateway.services.run_control"):
            status = run_control.session_status(
                "team-session-recovered",
                db=db,
                current_gateway_instance_id="current-gateway",
            )

        assert status["running"] is False
        assert "[dovie-run-control] orphaned-active-runs-recovered" in caplog.text
        stale = db.runs.get("stale-run")
        assert stale["status"] == "failed"
        assert stale["error"] == "gateway process restarted before run reached terminal state"
        assert stale["metadata"]["recovery_decision"] == (
            "same-pid-different-gateway-instance"
        )
        assert stale["terminal_seq"] > 0
        assert stale["terminal_cause"] == "worker_crashed"
        assert db.runs.list_events("team-session-recovered")[-1]["type"] == "error"
    finally:
        db.close()


def test_run_control_session_status_does_not_warn_for_live_owner_run(tmp_path, caplog):
    from hermes_agent.composition.cli_session_store import open_cli_session_store
    from tui_gateway.services import run_control

    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("team-session-live", "tui")
        db.runs.upsert(
            run_id="live-run",
            session_id="team-session-live",
            runtime_scope_key="team:conversation:leader",
            execution_session_id="runtime-live",
            status="running",
            metadata={
                "gateway_pid": os.getpid(),
                "gateway_instance_id": "current-gateway",
            },
        )

        with caplog.at_level(logging.WARNING, logger="tui_gateway.services.run_control"):
            status = run_control.session_status(
                "team-session-live",
                db=db,
                current_gateway_instance_id="current-gateway",
            )

        assert status["running"] is True
        assert db.runs.get("live-run")["status"] == "running"
        assert "[dovie-run-recovery] active-run-scan" not in caplog.text
    finally:
        db.close()


def test_run_control_session_status_does_not_fail_fresh_dead_owner_run(tmp_path, caplog):
    from hermes_agent.composition.cli_session_store import open_cli_session_store
    from tui_gateway.services import run_control

    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("team-session-fresh", "tui")
        db.runs.upsert(
            run_id="fresh-run",
            session_id="team-session-fresh",
            runtime_scope_key="profile:agent-default:version:v1",
            execution_session_id="runtime-fresh",
            status="running",
            metadata={
                "gateway_pid": 999_999_998,
                "gateway_instance_id": "previous-gateway",
            },
        )

        with caplog.at_level(logging.WARNING, logger="hermes_state_runs"):
            status = run_control.session_status(
                "team-session-fresh",
                db=db,
                current_gateway_instance_id="current-gateway",
            )

        assert status["running"] is True
        assert db.runs.get("fresh-run")["status"] == "running"
        assert "owner-pid-dead-fresh" in caplog.text or "[dovie-run-recovery] active-run-scan" not in caplog.text
    finally:
        db.close()


def test_run_control_session_status_recovers_recent_dead_owner_run(tmp_path, caplog):
    from hermes_agent.composition.cli_session_store import open_cli_session_store
    from tui_gateway.services import run_control

    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("team-session-recent", "tui")
        recent_time = time.time() - 30
        db.runs.upsert(
            run_id="recent-run",
            session_id="team-session-recent",
            runtime_scope_key="team:conversation:leader",
            execution_session_id="runtime-recent",
            status="running",
            started_at=recent_time,
            updated_at=recent_time,
            metadata={
                "gateway_pid": 999_999_998,
                "gateway_instance_id": "previous-gateway",
            },
        )

        with caplog.at_level(logging.WARNING, logger="hermes_state_runs"):
            status = run_control.session_status(
                "team-session-recent",
                db=db,
                current_gateway_instance_id="current-gateway",
            )

        assert status["running"] is False
        recovered = db.runs.get("recent-run")
        assert recovered["status"] == "failed"
        assert recovered["metadata"]["recovery_decision"] == "owner-pid-dead"
        assert "[dovie-run-control] orphaned-active-runs-recovered" in caplog.text
    finally:
        db.close()


def test_dead_owner_metadata_wins_over_stale_live_runtime_session_snapshot(tmp_path):
    from hermes_agent.composition.cli_session_store import open_cli_session_store

    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("team-session-stale-live", "tui")
        recent_time = time.time() - 30
        db.runs.upsert(
            run_id="dead-owner-run",
            session_id="team-session-stale-live",
            runtime_scope_key="team:conversation:leader",
            execution_session_id="runtime-stale-live",
            status="running",
            started_at=recent_time,
            updated_at=recent_time,
            metadata={
                "gateway_pid": 999_999_998,
                "gateway_instance_id": "dead-worker",
            },
        )

        failed = db.runs.fail_orphaned(
            live_execution_session_ids={"runtime-stale-live"},
            current_pid=os.getpid(),
            current_gateway_instance_id="current-gateway",
            stale_after_seconds=300,
            owner_dead_grace_seconds=2,
        )

        assert failed == 1
        recovered = db.runs.get("dead-owner-run")
        assert recovered["status"] == "failed"
        assert recovered["error"] == "runtime owner is no longer available"
    finally:
        db.close()


def test_run_control_reservation_recovers_dead_gateway_active_run(tmp_path):
    from hermes_agent.composition.cli_session_store import open_cli_session_store
    from tui_gateway.services import run_control

    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("team-session-submit", "tui")
        stale_time = time.time() - 301
        db.runs.upsert(
            run_id="stale-run",
            session_id="team-session-submit",
            runtime_scope_key="team:conversation:leader",
            execution_session_id="runtime-stale",
            status="running",
            started_at=stale_time,
            updated_at=stale_time,
            metadata={
                "gateway_pid": os.getpid(),
                "gateway_instance_id": "previous-gateway",
            },
        )

        reservation = run_control.create_run_if_session_idle(
            conversation_session_id="team-session-submit",
            run_id="next-run",
            turn_id="next-turn",
            runtime_scope_key="team:conversation:leader",
            execution_session_id="runtime-current",
            metadata={
                "gateway_pid": os.getpid(),
                "gateway_instance_id": "current-gateway",
            },
            db=db,
        )

        assert reservation["created"] is True
        assert reservation["conflict"] is None
        assert reservation["run"]["run_id"] == "next-run"
        assert db.runs.get("stale-run")["status"] == "failed"
    finally:
        db.close()


def test_run_submit_recovers_dead_gateway_active_run_before_busy_check(tmp_path, monkeypatch):
    import importlib

    from hermes_agent.composition.cli_session_store import open_cli_session_store
    from tui_gateway import server

    run_methods = importlib.import_module("tui_gateway.methods.run")
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("team-session-submit", "tui")
        stale_time = time.time() - 301
        db.runs.upsert(
            run_id="stale-run",
            session_id="team-session-submit",
            runtime_scope_key="team:conversation:leader",
            execution_session_id="runtime-stale",
            status="running",
            started_at=stale_time,
            updated_at=stale_time,
            metadata={
                "gateway_pid": os.getpid(),
                "gateway_instance_id": "previous-gateway",
            },
        )
        monkeypatch.setattr(run_methods, "_get_db", lambda: db)
        monkeypatch.setattr(
            run_methods,
            "_runtime_for_run_target",
            lambda _rid, _params: (
                "runtime-current",
                {"session_key": "team-session-submit"},
                None,
            ),
        )

        def fake_prompt_submit(rid, params):
            return {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "status": "running",
                    "run_id": params["run_id"],
                    "turn_id": params["turn_id"],
                    "conversation_session_id": params["conversation_session_id"],
                },
            }

        monkeypatch.setitem(server._methods, "prompt.submit", fake_prompt_submit)

        response = server._methods["run.submit"](
            1,
            {
                "conversation_session_id": "team-session-submit",
                "client_run_id": "next-run",
                "turn_id": "next-turn",
                "runtime_scope_key": "team:conversation:leader",
                "text": "你好",
                "persist_user_message": "你好",
            },
        )

        assert "error" not in response
        assert response["result"]["run_id"] == "next-run"
        assert db.runs.get("stale-run")["status"] == "failed"
        assert db.runs.get("next-run")["status"] == "running"
    finally:
        db.close()


def test_gateway_shutdown_terminalizes_active_run(tmp_path, monkeypatch):
    from hermes_agent.composition.cli_session_store import open_cli_session_store
    from tui_gateway import server

    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("team-session-shutdown", "tui")
        db.runs.upsert(
            run_id="active-run",
            session_id="team-session-shutdown",
            runtime_scope_key="team:conversation:leader",
            execution_session_id="runtime-live",
            turn_id="turn-1",
            status="running",
            metadata={
                "gateway_pid": os.getpid(),
                "gateway_instance_id": "current-gateway",
            },
        )
        monkeypatch.setattr(server, "_get_db", lambda: db)

        session = {
            "session_key": "team-session-shutdown",
            "history": [],
            "history_lock": threading.Lock(),
            "running": True,
            "active_run_id": "active-run",
            "active_turn_id": "turn-1",
            "active_runtime_scope_key": "team:conversation:leader",
        }

        server._finalize_session(
            session,
            end_reason="test_shutdown",
            runtime_sid="runtime-live",
        )

        run = db.runs.get("active-run")
        assert run["status"] == "interrupted"
        assert run["error"] == "gateway test_shutdown before run reached terminal state"
        assert db.runs.list_events("team-session-shutdown")[-1]["type"] == "message.complete"
        assert db.runs.list_events("team-session-shutdown")[-1]["payload"]["status"] == "interrupted"
    finally:
        db.close()


def test_team_conversation_resolve_recovers_dead_gateway_active_run(tmp_path, monkeypatch):
    import importlib

    from hermes_agent.composition.cli_session_store import open_cli_session_store
    from tui_gateway import server

    team_mission = team_mission_gateway()
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.upsert_team_mission_conversation(
            conversation_id="conversation-1",
            conversation_session_id="team-session-resolve",
            team_id="team-1",
            title="团队会话",
        )
        stale_time = time.time() - 301
        db.runs.upsert(
            run_id="stale-run",
            session_id="team-session-resolve",
            runtime_scope_key="team:conversation:leader",
            execution_session_id="runtime-stale",
            status="running",
            started_at=stale_time,
            updated_at=stale_time,
            metadata={
                "gateway_pid": os.getpid(),
                "gateway_instance_id": "previous-gateway",
            },
        )
        monkeypatch.setattr(team_mission, "_get_db", lambda: db)

        response = server._methods["team_mission.conversation.resolve"](
            1,
            {"identifier": "conversation-1"},
        )

        assert response["result"]["conversation"]["conversation_session_id"] == "team-session-resolve"
        assert db.runs.get("stale-run")["status"] == "failed"
    finally:
        db.close()


def test_session_db_does_not_open_active_run_for_team_mission_control_events(tmp_path):
    from hermes_agent.composition.cli_session_store import open_cli_session_store

    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("team-session-1", "tui")
        db.runs.append_event(
            "team-session-1",
            {
                "type": "mission.approval.requested",
                "session_id": "runtime-control",
                "run_id": "team-mission:mission-1:conversation:plan",
                "runtime_scope_key": "team_mission:mission-1",
                "seq": 1,
                "payload": {"mission_id": "mission-1"},
            },
        )
        db.runs.append_event(
            "team-session-1",
            {
                "type": "mission.strategy.actions",
                "session_id": "runtime-control",
                "run_id": "team-mission:mission-1:conversation:plan",
                "runtime_scope_key": "team_mission:mission-1",
                "seq": 2,
                "payload": {"mission_id": "mission-1"},
            },
        )

        assert db.runs.get("team-mission:mission-1:conversation:plan") is None
        assert db.runs.session_status("team-session-1")["running"] is False

        next_run = db.runs.reserve_if_idle(
            run_id="leader-run-2",
            session_id="team-session-1",
            runtime_scope_key="team:conversation-1:leader-conversation",
            turn_id="leader-turn-2",
            execution_session_id="runtime-2",
        )
        assert next_run["created"] is True
        assert next_run["conflict"] is None
    finally:
        db.close()


def test_session_db_does_not_repair_live_control_only_team_leader_run(tmp_path):
    from hermes_agent.composition.cli_session_store import open_cli_session_store

    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("team-session-1", "tui")
        db.runs.upsert(
            run_id="team-leader-run-1",
            session_id="team-session-1",
            runtime_scope_key="team:team-conversation-1:leader-conversation",
            turn_id="turn-1",
            execution_session_id="runtime-leader",
            status="running",
            metadata={
                "gateway_pid": os.getpid(),
                "gateway_instance_id": "test-live-worker",
            },
        )
        db.runs.append_event(
            "team-session-1",
            {
                "type": "session.info",
                "session_id": "runtime-leader",
                "conversation_session_id": "team-session-1",
                "run_id": "team-leader-run-1",
                "turn_id": "turn-1",
                "runtime_scope_key": "team:team-conversation-1:leader-conversation",
                "seq": 1,
                "payload": {"status": "starting"},
            },
        )

        status = db.runs.session_status("team-session-1")
        run = db.runs.get("team-leader-run-1")

        assert status["running"] is True
        assert status["active_run_id"] == "team-leader-run-1"
        assert status["runtime_scope_key"] == "team:team-conversation-1:leader-conversation"
        assert run["status"] == "running"
        assert "recovery_reason" not in run["metadata"]

        next_run = db.runs.reserve_if_idle(
            run_id="team-leader-run-2",
            session_id="team-session-1",
            runtime_scope_key="team:team-conversation-1:leader-conversation",
            turn_id="turn-2",
            execution_session_id="runtime-leader-2",
        )
        assert next_run["created"] is False
        assert next_run["run"] is None
        assert next_run["conflict"]["run_id"] == "team-leader-run-1"
    finally:
        db.close()


def test_session_db_replays_run_events_by_runtime_scope(tmp_path):
    from hermes_agent.composition.cli_session_store import open_cli_session_store

    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.sessions.create("session-1", "tui")
        for run_id, scope, seq, text in (
            ("run-v1", "profile:agent-a:version:v1", 1, "v1"),
            ("run-v2", "profile:agent-a:version:v2", 2, "v2"),
            ("run-default", "profile:agent-default", 3, "default"),
        ):
            db.runs.append_event(
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

        v1_events = db.runs.list_events(
            "session-1",
            runtime_scope_key="profile:agent-a:version:v1",
        )
        assert [item["run_id"] for item in v1_events] == ["run-v1"]
        assert [item["payload"]["text"] for item in v1_events] == ["v1"]

        v2_events = db.runs.list_events(
            "session-1",
            runtime_scope_key="profile:agent-a:version:v2",
        )
        assert [item["run_id"] for item in v2_events] == ["run-v2"]
        assert [
            item["payload"]["text"]
            for item in db.runs.list_events("session-1", run_id="run-v2")
        ] == ["v2"]

        assert db.runs.list_events(
            "session-1",
            after_seq=2,
            runtime_scope_key="profile:agent-a:version:v1",
        ) == []
        assert [
            item["run_id"]
            for item in db.runs.list_events("session-1", after_seq=2)
        ] == ["run-default"]
    finally:
        db.close()
