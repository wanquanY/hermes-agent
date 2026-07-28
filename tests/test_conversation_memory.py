from __future__ import annotations

from pathlib import Path

import pytest

from hermes_agent.domain.conversation_memory import MemoryAccessContext
from hermes_agent.composition.cli_session_store import open_cli_session_store


def _db(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("conversation-1", source="team_mission", transient=False)
    db.participants.ensure_participant(
        "conversation-1",
        participant_id="leader:conversation-1",
        role="leader",
        agent_profile_id="profile-leader",
    )
    db.participants.ensure_participant(
        "conversation-1",
        participant_id="member:a",
        role="member",
        agent_profile_id="profile-shared",
    )
    db.participants.ensure_participant(
        "conversation-1",
        participant_id="member:b",
        role="member",
        agent_profile_id="profile-shared",
    )
    return db


def test_participant_memory_isolated_even_when_members_share_profile(tmp_path: Path) -> None:
    db = _db(tmp_path)
    memory = db.conversation_memory.create_item(
        conversation_session_id="conversation-1",
        owner_kind="participant",
        owner_id="member:a",
        participant_id="member:a",
        kind="commitment",
        content="A promised to verify the build.",
        visibility={"kind": "private", "participant_id": "member:a"},
        provenance={"source_event_ids": ["event-a"]},
        status="committed",
    )

    a_items = db.conversation_memory.list_visible(
        MemoryAccessContext(
            conversation_session_id="conversation-1",
            actor_participant_id="member:a",
            actor_role="member",
            profile_id="profile-shared",
        )
    )
    b_items = db.conversation_memory.list_visible(
        MemoryAccessContext(
            conversation_session_id="conversation-1",
            actor_participant_id="member:b",
            actor_role="member",
            profile_id="profile-shared",
        )
    )

    assert [item["memory_id"] for item in a_items] == [memory["memory_id"]]
    assert b_items == []


def test_shared_and_activity_memory_respect_visibility(tmp_path: Path) -> None:
    db = _db(tmp_path)
    shared = db.conversation_memory.create_item(
        conversation_session_id="conversation-1",
        owner_kind="conversation",
        owner_id="conversation-1",
        kind="decision",
        content="The team selected option A.",
        visibility={"kind": "conversation"},
        status="committed",
    )
    activity = db.conversation_memory.create_item(
        conversation_session_id="conversation-1",
        owner_kind="activity",
        owner_id="mission:one",
        activity_id="mission:one",
        kind="constraint",
        content="Mission one must remain read-only.",
        visibility={"kind": "activity", "activity_id": "mission:one"},
        status="committed",
    )

    chat_items = db.conversation_memory.list_visible(
        MemoryAccessContext(
            conversation_session_id="conversation-1",
            actor_participant_id="member:a",
            actor_role="member",
            profile_id="profile-shared",
        )
    )
    mission_items = db.conversation_memory.list_visible(
        MemoryAccessContext(
            conversation_session_id="conversation-1",
            actor_participant_id="member:a",
            actor_role="member",
            activity_id="mission:one",
            profile_id="profile-shared",
        )
    )

    assert [item["memory_id"] for item in chat_items] == [shared["memory_id"]]
    assert {item["memory_id"] for item in mission_items} == {
        shared["memory_id"],
        activity["memory_id"],
    }


def test_visible_memory_resolver_surfaces_conflicts_without_dropping_candidates(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    created = []
    for content in ("Launch in Germany", "Launch in France"):
        created.append(
            db.conversation_memory.create_item(
                conversation_session_id="conversation-1",
                owner_kind="conversation",
                owner_id="conversation-1",
                kind="decision",
                content=content,
                structured_payload={"conflict_key": "launch_region"},
                visibility={"kind": "conversation"},
                status="committed",
            )
        )
    resolved = db.conversation_memory.resolve_visible(
        MemoryAccessContext(
            conversation_session_id="conversation-1",
            actor_participant_id="member:a",
            actor_role="member",
        )
    )

    assert {item["memory_id"] for item in resolved["items"]} == {
        item["memory_id"] for item in created
    }
    assert resolved["conflicts"][0]["conflict_key"] == "decision:launch_region"
    assert set(resolved["conflicts"][0]["memory_item_ids"]) == {
        item["memory_id"] for item in created
    }


def test_memory_transition_uses_revision_cas(tmp_path: Path) -> None:
    db = _db(tmp_path)
    proposed = db.conversation_memory.create_item(
        conversation_session_id="conversation-1",
        owner_kind="conversation",
        owner_id="conversation-1",
        kind="fact",
        content="Candidate fact.",
        visibility={"kind": "conversation"},
    )

    committed = db.conversation_memory.transition_item(
        proposed["memory_id"],
        expected_revision=1,
        status="committed",
    )
    assert committed["status"] == "committed"
    assert committed["revision"] == 2

    with pytest.raises(RuntimeError, match="revision conflict"):
        db.conversation_memory.transition_item(
            proposed["memory_id"],
            expected_revision=1,
            status="invalidated",
        )


def test_actor_summary_commit_advances_only_target_participant(tmp_path: Path) -> None:
    db = _db(tmp_path)
    snapshot = db.conversation_memory.create_snapshot(
        conversation_session_id="conversation-1",
        actor_participant_id="member:a",
        execution_scope_key="member-chat:conversation-1:member:a",
        conversation_revision=12,
        transcript_cursor=0,
        selected_event_ids=["event-1", "event-2"],
    )
    summary = db.conversation_memory.commit_actor_summary(
        conversation_session_id="conversation-1",
        actor_participant_id="member:a",
        snapshot_id=snapshot["snapshot_id"],
        expected_memory_revision=0,
        from_seq=1,
        to_seq=12,
        conversation_revision=12,
        summary={
            "actor_statements": [],
            "other_participant_statements": [
                {"participant_id": "member:b", "summary": "B owns testing."}
            ],
        },
        source_event_ids=["event-1", "event-2"],
    )

    assert summary["revision"] == 1
    assert summary["to_seq"] == 12
    assert summary["summary"]["other_participant_statements"][0]["participant_id"] == "member:b"
    assert db.participants.get_participant("conversation-1", "member:a")["memory_revision"] == 1
    assert db.participants.get_participant("conversation-1", "member:a")["transcript_cursor"] == 12
    assert db.participants.get_participant("conversation-1", "member:b")["memory_revision"] == 0

    with pytest.raises(RuntimeError, match="revision conflict"):
        db.conversation_memory.commit_actor_summary(
            conversation_session_id="conversation-1",
            actor_participant_id="member:a",
            snapshot_id=snapshot["snapshot_id"],
            expected_memory_revision=0,
            from_seq=13,
            to_seq=14,
            conversation_revision=14,
            summary={},
            source_event_ids=["event-3"],
        )

    invalidated = db.conversation_memory.invalidate_summaries_for_events(
        ["event-2"],
        reason="source message retracted",
    )
    assert invalidated == [summary["summary_id"]]
    assert db.conversation_memory.latest_actor_summary("conversation-1", "member:a") == {}
    rewound = db.participants.get_participant("conversation-1", "member:a")
    assert rewound["transcript_cursor"] == 0
    assert rewound["memory_revision"] == 2


def test_summary_invalidation_also_invalidates_proposed_derived_memory(tmp_path: Path) -> None:
    db = _db(tmp_path)
    proposed = db.conversation_memory.create_item(
        conversation_session_id="conversation-1",
        owner_kind="activity",
        owner_id="mission:one",
        activity_id="mission:one",
        kind="fact",
        content="Unverified node observation",
        visibility={"kind": "activity", "activity_id": "mission:one"},
        provenance={"source_event_ids": ["event-retracted"]},
        status="proposed",
    )

    invalidated = db.conversation_memory.invalidate_summaries_for_events(
        ["event-retracted"],
        reason="source message retracted",
    )

    assert proposed["memory_id"] in invalidated
    assert db.conversation_memory.get_item(proposed["memory_id"])["status"] == "invalidated"


def test_activity_context_snapshot_is_revisioned_and_immutable(tmp_path: Path) -> None:
    db = _db(tmp_path)
    first = db.conversation_memory.create_activity_snapshot(
        conversation_session_id="conversation-1",
        activity_id="mission:one",
        objective="Original objective",
        conversation_revision=7,
        selected_event_ids=["event-1"],
        selected_memory_ids=["memory-1"],
        team_snapshot={"snapshot_id": "team-v1"},
        workspace_snapshot={"workspace_id": "workspace-1"},
        expected_revision=0,
    )

    assert first["activity_context_revision"] == 1
    assert first["selected_memory_ids"] == ["memory-1"]
    assert db.conversation_memory.latest_activity_snapshot("mission:one")["snapshot_id"] == first["snapshot_id"]

    with pytest.raises(RuntimeError, match="revision conflict"):
        db.conversation_memory.create_activity_snapshot(
            conversation_session_id="conversation-1",
            activity_id="mission:one",
            objective="Implicitly changed objective",
            conversation_revision=8,
            expected_revision=0,
        )

    changed = db.conversation_memory.create_activity_snapshot(
        conversation_session_id="conversation-1",
        activity_id="mission:one",
        objective="Explicit change request objective",
        conversation_revision=8,
        selected_event_ids=["event-1", "event-change"],
        selected_memory_ids=["memory-1", "memory-change"],
        expected_revision=1,
    )
    assert changed["activity_context_revision"] == 2
    assert changed["supersedes_snapshot_id"] == first["snapshot_id"]


def test_activity_context_change_gateway_requires_leader_and_creates_revision(
    monkeypatch, tmp_path: Path
) -> None:
    from tui_gateway import server
    from tui_gateway.methods import conversation_activity

    db = _db(tmp_path)
    db.participants.ensure_participant(
        "conversation-1", participant_id="leader:one", role="leader"
    )
    db.participants.ensure_participant(
        "conversation-1", participant_id="member:one", role="member"
    )
    first = db.conversation_memory.create_activity_snapshot(
        conversation_session_id="conversation-1",
        activity_id="mission:one",
        objective="Original objective",
        conversation_revision=1,
        expected_revision=0,
    )
    monkeypatch.setattr(conversation_activity, "_get_db", lambda: db)

    forbidden = server._methods["conversation.activity.context.change"](
        1,
        {
            "conversation_session_id": "conversation-1",
            "participant_id": "member:one",
            "activity_id": "mission:one",
            "objective": "Unauthorized change",
        },
    )
    assert forbidden["error"]["code"] == 4030

    changed = server._methods["conversation.activity.context.change"](
        2,
        {
            "conversation_session_id": "conversation-1",
            "participant_id": "leader:one",
            "activity_id": "mission:one",
            "objective": "Explicitly revised objective",
            "expected_revision": first["activity_context_revision"],
        },
    )["result"]["snapshot"]
    assert changed["activity_context_revision"] == 2
    assert changed["supersedes_snapshot_id"] == first["snapshot_id"]
