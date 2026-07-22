from __future__ import annotations

import threading
from pathlib import Path

from hermes_agent.composition.cli_session_store import open_cli_session_store
from tui_gateway.services.team_mission_activity_events import (
    activity_last_seq,
    deliver_appended_event,
    list_activity_events,
    mission_id_for_activity,
    mission_status_for_activity,
    uses_mission_activity_journal,
)


def test_activity_replay_queries_use_store_components(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.upsert_team_mission(
            mission_id="mission-1",
            conversation_id="conversation-1",
            team_id="team-1",
            title="Mission",
            objective="Verify activity replay ownership",
            status="running",
            leader_session_id="team-session-1",
        )
        db.activities.create(
            activity_id="act-team_dispatch-1",
            conversation_id="team-session-1",
            kind="team_dispatch",
        )
        db.activities.bind_to_mission(
            activity_id="act-team_dispatch-1",
            conversation_id="team-session-1",
            mission_id="mission-1",
        )

        db.runs.append_event(
            "team:mission:mission-1:events",
            {
                "type": "mission.node.created",
                "run_id": "run-1",
                "activity_id": "mission:mission-1",
                "payload": {"mission_id": "mission-1"},
            },
            activity_id="mission:mission-1",
        )
        db.runs.append_event(
            "team:mission:mission-1:events",
            {
                "type": "mission.node.updated",
                "activity_id": "mission:mission-1",
                "payload": {"mission_id": "mission-1"},
            },
            activity_id="mission:mission-1",
        )
        # A legacy projection may carry the same mission activity id inside a
        # node-local sequence domain. It must never participate in the mission
        # cursor after the dedicated activity ledger is authoritative.
        db.runs.append_event(
            "team:mission-1:node:legacy",
            {
                "type": "mission.node.created",
                "activity_id": "mission:mission-1",
                "payload": {"mission_id": "mission-1", "legacy": True},
            },
            activity_id="mission:mission-1",
        )
        db.runs.append_event(
            "chat-session-1",
            {
                "type": "message.complete",
                "run_id": "chat-run-1",
                "payload": {"text": "done"},
            },
        )

        assert mission_id_for_activity("act-team_dispatch-1", db=db) == "mission-1"
        assert uses_mission_activity_journal("mission:mission-1", db=db) is True
        assert mission_status_for_activity("mission:mission-1", db=db) == "running"
        assert activity_last_seq("mission:mission-1", db=db) == 2
        assert activity_last_seq("chat:chat-session-1", db=db) == 1
    finally:
        db.close()


def test_unknown_mission_does_not_select_mission_replay(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        assert uses_mission_activity_journal("mission:missing", db=db) is False
        assert mission_status_for_activity("mission:missing", db=db) == ""
        assert activity_last_seq("chat:missing", db=db) == 0
    finally:
        db.close()


def test_node_activity_replay_uses_canonical_mission_journal_cursor(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.upsert_team_mission(
            mission_id="mission-node-stream",
            conversation_id="conversation-aggregate",
            team_id="team-1",
            title="Mission",
            objective="Stream one node",
            status="running",
            leader_session_id="conversation-owner",
        )
        activity_id = "act-node:mission-node-stream:worker-a"

        # This node-local row has its own session-local seq=1.  It is persisted
        # execution history, not the ordered Activity transport journal.
        db.runs.append_event(
            "team:mission-node-stream:node:worker-a",
            {
                "type": "message.complete",
                "run_id": "run-worker-a",
                "activity_id": activity_id,
                "payload": {"text": "node-local duplicate"},
            },
            activity_id=activity_id,
        )

        def append_mission_event(node_id: str, text: str) -> None:
            db.runs.append_event(
                "team:mission:mission-node-stream:events",
                {
                    "type": "team_mission.runtime.event",
                    "activity_id": "mission:mission-node-stream",
                    "payload": {
                        "kind": "node.output.delta",
                        "source_event_type": "message.delta",
                        "mission_id": "mission-node-stream",
                        "subject": {
                            "type": "node",
                            "id": node_id,
                            "node_id": node_id,
                            "mission_id": "mission-node-stream",
                            # Event subjects own execution identity.  The
                            # transport owner must come from team_missions,
                            # even when this historical field is polluted.
                            "conversation_session_id": "mission-node-stream",
                            "runtime_conversation_session_id": (
                                f"team:mission-node-stream:node:{node_id}"
                            ),
                        },
                        "text_stream": {
                            "event": "delta",
                            "stream_id": f"stream-{node_id}",
                            "mode": "append",
                            "offset": 0,
                            "delta": text,
                        },
                        "run_id": f"run-{node_id}",
                    },
                },
                activity_id="mission:mission-node-stream",
            )

        append_mission_event("worker-a", "A")
        append_mission_event("worker-b", "B")

        replay = list_activity_events(
            db,
            activity_id,
            after_seq=0,
            limit=100,
            event_activity_id=lambda event: str(event.get("activity_id") or ""),
        )

        assert len(replay) == 1
        assert replay[0]["seq"] == 1
        assert replay[0]["activity_event_seq"] == 1
        assert replay[0]["activity_id"] == activity_id
        assert replay[0]["conversation_session_id"] == "conversation-owner"
        assert replay[0]["payload"]["text_stream"]["delta"] == "A"
        # The Activity watermark follows the canonical mission journal, not
        # either node runtime's unrelated local seq domain.
        assert activity_last_seq(activity_id, db=db) == 2
    finally:
        db.close()


def test_node_activity_live_delivery_uses_aggregate_conversation_owner(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    try:
        db.upsert_team_mission(
            mission_id="mission-live",
            conversation_id="conversation-aggregate",
            team_id="team-1",
            title="Mission",
            objective="Stream one live node",
            status="running",
            leader_session_id="conversation-owner",
        )
        activity_id = "act-node:mission-live:worker-a"
        delivered: list[dict[str, object]] = []

        deliver_appended_event(
            "mission-live",
            {
                "type": "team_mission.runtime.event",
                "seq": 7,
                "activity_id": "mission:mission-live",
                "payload": {
                    "kind": "node.output.delta",
                    "source_event_type": "message.delta",
                    "mission_id": "mission-live",
                    "subject": {
                        "type": "node",
                        "id": "worker-a",
                        "node_id": "worker-a",
                        "mission_id": "mission-live",
                        "conversation_session_id": "mission-live",
                    },
                    "text_stream": {
                        "event": "delta",
                        "stream_id": "stream-worker-a",
                        "mode": "append",
                        "offset": 0,
                        "delta": "A",
                    },
                },
            },
            lock=threading.RLock(),
            subscription_ids_by_activity={activity_id: {"subscription-1"}},
            subscriptions_by_id={
                "subscription-1": {
                    "id": "subscription-1",
                    "db": db,
                    "transport": object(),
                }
            },
            live_status_event_for_subscription=lambda _subscription, event: event,
            deliver_subscription_event=lambda _subscription_id, _transport, event: (
                delivered.append(event) or True
            ),
        )

        assert len(delivered) == 1
        assert delivered[0]["conversation_session_id"] == "conversation-owner"
        assert delivered[0]["session_id"] == "conversation-owner"
        payload = delivered[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["conversation_session_id"] == "conversation-owner"
        assert payload["text_stream"]["delta"] == "A"
    finally:
        db.close()
