from pathlib import Path

from hermes_agent.composition.cli_session_store import CliSessionStore, open_cli_session_store


def _ids(result):
    return [s["session_id"] for s in result["sessions"]]


def test_upsert_and_list_roundtrip(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.session_index.upsert(
        session_id="s1",
        owner_agent_profile_id="agent-7",
        title="Hello",
        preview="hi there",
        source="cli",
        running=True,
        status="running",
        active_run_id="run-1",
        started_at=100.0,
        updated_at=100.0,
    )
    result = db.session_index.list()
    assert _ids(result) == ["s1"]
    item = result["sessions"][0]
    assert item["owner_agent_profile_id"] == "agent-7"
    assert item["title"] == "Hello"
    assert item["running"] is True  # cast to bool
    assert item["transient"] is False
    assert item["status"] == "running"
    assert item["active_run_id"] == "run-1"


def test_session_index_aggregation_plain_session_has_null_context(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.session_index.upsert(
        session_id="plain-session",
        source="cli",
        conversation_kind="direct",
        title="Plain",
        started_at=1.0,
        updated_at=1.0,
    )

    item = db.session_index.list()["sessions"][0]

    assert item["team_context"] is None
    assert item["derived_state"] == {
        "running": False,
        "waiting_approval": False,
        "terminal_status": None,
    }


def test_session_index_aggregation_team_context_from_mission_tables(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        conversation_id="conversation-1",
        team_id="team-1",
        title="Mission",
        mode="supervised_mission",
        status="running",
    )
    db.upsert_team_mission_conversation(
        conversation_id="conversation-1",
        team_id="team-1",
        conversation_session_id="team-session-1",
        title="Team",
        active_mission_id="mission-1",
        created_at=1.0,
        updated_at=2.0,
    )

    item = db.session_index.list()["sessions"][0]

    assert item["team_context"] == {
        "team_id": "team-1",
        "team_conversation_id": "conversation-1",
        "mission_id": "mission-1",
        "member_id": "",
    }
    assert item["derived_state"] == {
        "running": True,
        "waiting_approval": False,
        "terminal_status": None,
    }


def test_session_index_aggregation_member_context_from_participants(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-member",
        conversation_id="conversation-member",
        team_id="team-member",
        title="Mission",
        mode="supervised_mission",
        status="running",
    )
    db.upsert_team_mission_conversation(
        conversation_id="conversation-member",
        team_id="team-member",
        conversation_session_id="team-session-member",
        title="Team",
        active_mission_id="mission-member",
        created_at=1.0,
        updated_at=2.0,
    )
    db.participants.upsert_conversation_participant(
        conversation_session_id="team-session-member",
        participant_id="member:m-alice",
        role="member",
        member_id="m-alice",
        runtime_scope_key="member-chat:conversation-member:m-alice",
    )
    db.session_index.upsert(
        session_id="member-session",
        source="team_mission",
        session_kind="team_mission",
        conversation_kind="team",
        team_id="team-member",
        mission_id="mission-member",
        conversation_id="conversation-member",
        runtime_scope_key="member-chat:conversation-member:m-alice",
        title="Member",
        started_at=3.0,
        updated_at=3.0,
    )

    by_id = {item["session_id"]: item for item in db.session_index.list()["sessions"]}

    assert by_id["member-session"]["team_context"] == {
        "team_id": "team-member",
        "team_conversation_id": "conversation-member",
        "mission_id": "mission-member",
        "member_id": "m-alice",
    }


def test_session_index_aggregation_waiting_approval_from_pending_gate(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-approval",
        conversation_id="conversation-approval",
        team_id="team-approval",
        title="Mission",
        mode="supervised_mission",
        status="running",
    )
    db.upsert_team_mission_conversation(
        conversation_id="conversation-approval",
        team_id="team-approval",
        conversation_session_id="team-session-approval",
        title="Team",
        active_mission_id="mission-approval",
        created_at=1.0,
        updated_at=2.0,
    )
    db.upsert_team_mission_node(
        mission_id="mission-approval",
        node_id="approval",
        kind="approval_gate",
        title="Approve",
        status="waiting_approval",
    )

    item = db.session_index.list()["sessions"][0]

    assert item["waiting_approval"] is False
    assert item["derived_state"]["waiting_approval"] is True
    assert item["derived_state"]["running"] is False


def test_upsert_is_idempotent_by_id(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.session_index.upsert(session_id="s1", title="v1", started_at=1.0, updated_at=1.0)
    db.session_index.upsert(session_id="s1", title="v2", running=True, started_at=1.0, updated_at=2.0)
    result = db.session_index.list()
    assert len(result["sessions"]) == 1
    assert result["sessions"][0]["title"] == "v2"
    assert result["sessions"][0]["running"] is True


def test_transient_excluded_by_default(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.session_index.upsert(session_id="real", started_at=1.0, updated_at=1.0)
    db.session_index.upsert(session_id="draft", transient=True, started_at=2.0, updated_at=2.0)
    assert _ids(db.session_index.list()) == ["real"]
    assert set(_ids(db.session_index.list(include_transient=True))) == {"real", "draft"}


def test_ordering_newest_first(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.session_index.upsert(session_id="old", started_at=1.0, updated_at=1.0)
    db.session_index.upsert(session_id="new", started_at=3.0, updated_at=3.0)
    db.session_index.upsert(session_id="mid", started_at=2.0, updated_at=2.0)
    assert _ids(db.session_index.list()) == ["new", "mid", "old"]


def test_list_is_a_pure_read_and_does_not_open_a_unit_of_work(tmp_path: Path, monkeypatch):
    db = open_cli_session_store(tmp_path / "state.db")
    db.session_index.upsert(session_id="s1", started_at=1.0, updated_at=1.0)

    monkeypatch.setattr(
        db.session_index._unit_of_work,
        "execute",
        lambda _operation: (_ for _ in ()).throw(AssertionError("list opened a write transaction")),
    )

    assert _ids(db.session_index.list()) == ["s1"]


def test_keyset_pagination_no_dupes_full_coverage(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    for i in range(10):
        db.session_index.upsert(session_id=f"s{i:02d}", started_at=float(i), updated_at=float(i))
    seen = []
    cursor = None
    for _ in range(10):
        page = db.session_index.list(limit=3, cursor=cursor)
        seen.extend(_ids(page))
        cursor = page["pageInfo"]["nextCursor"]
        if not page["pageInfo"]["hasMore"]:
            break
    assert len(seen) == 10
    assert len(set(seen)) == 10  # no duplicates across pages
    assert seen == sorted(seen, reverse=True)  # newest-first preserved


def test_delete_removes_row(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.session_index.upsert(session_id="s1", started_at=1.0, updated_at=1.0)
    assert db.session_index.delete("s1") == 1
    assert db.session_index.list()["sessions"] == []


def test_reconcile_backfills_from_sessions_excluding_tool(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("conv-1", source="cli")
    db.sessions.create("tool-1", source="tool")
    result = db.session_index.reconcile()
    assert result["reconciled"] >= 1
    ids = _ids(db.session_index.list())
    assert "conv-1" in ids
    assert "tool-1" not in ids  # tool source excluded


def test_reconcile_preserves_live_status(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("conv-1", source="cli")
    db.session_index.upsert(
        session_id="conv-1", title="Conversation 1", source="cli",
        running=True, status="running", active_run_id="run-9",
        started_at=1.0, updated_at=5.0,
    )
    db.session_index.reconcile()
    item = db.session_index.list()["sessions"][0]
    # reconcile refreshes static fields but must NOT clobber live status
    assert item["running"] is True
    assert item["status"] == "running"
    assert item["active_run_id"] == "run-9"


def test_run_state_projects_into_session_index(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    # user-facing session has an index row (as session.create would create)
    db.session_index.upsert(session_id="s1", source="cli", started_at=1.0, updated_at=1.0)

    db.runs.upsert(run_id="run-1", session_id="s1", status="running", execution_session_id="rt-1")
    item = db.session_index.list()["sessions"][0]
    assert item["running"] is True
    assert item["status"] == "running"
    assert item["active_run_id"] == "run-1"
    assert item["active_execution_session_id"] == "rt-1"

    db.runs.upsert(run_id="run-1", session_id="s1", status="completed")
    item = db.session_index.list()["sessions"][0]
    assert item["running"] is False
    assert item["status"] == "idle"
    assert item["active_run_id"] == ""


def test_run_state_does_not_create_index_row_for_member_sessions(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    # no session_index row exists (e.g. team-member runtime session)
    db.runs.upsert(run_id="run-x", session_id="member-session", status="running")
    assert db.session_index.list(include_transient=True)["sessions"] == []


def test_append_run_event_terminal_clears_session_index_running(tmp_path: Path):
    """The streaming append_run_event path was missing the projection that
    upsert_run already has, so session_index kept `running=1, status='running'`
    after a `message.complete` terminal event closed the run. The sidebar
    showed every completed conversation as still spinning until the next app
    restart."""
    db = open_cli_session_store(tmp_path / "state.db")
    db.session_index.upsert(session_id="s-live", source="cli", started_at=1.0, updated_at=1.0)
    # Run-start path goes through the run-control gateway (upsert_run), which
    # already had its own projection — emulate that to set the session as
    # running/active.
    db.runs.upsert(run_id="run-A", session_id="s-live", status="running")
    item = db.session_index.list()["sessions"][0]
    assert item["running"] is True
    assert item["active_run_id"] == "run-A"

    # Terminal streaming event: `append_run_event` is the path the live
    # streaming pipeline uses to record run_events. Before the fix, this path
    # updated `runs.status` to `completed` but left `session_index.running=1`,
    # so the sidebar kept the spinner on every finished session.
    db.runs.append_event(
        "s-live",
        {
            "type": "message.complete",
            "run_id": "run-A",
            "turn_id": "turn-A",
            "seq": 2,
            "payload": {"status": "completed", "text": "done"},
        },
    )
    item = db.session_index.list()["sessions"][0]
    assert item["running"] is False
    assert item["status"] == "idle"
    assert item["active_run_id"] == ""


def test_append_run_event_terminal_prunes_message_delta_rows(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.runs.append_event(
        "s-live",
        {
            "type": "message.delta",
            "run_id": "run-A",
            "turn_id": "turn-A",
            "seq": 1,
            "payload": {"mode": "append", "delta": "hel"},
        },
    )
    db.runs.append_event(
        "s-live",
        {
            "type": "message.delta",
            "run_id": "run-A",
            "turn_id": "turn-A",
            "seq": 2,
            "payload": {"mode": "append", "delta": "lo"},
        },
    )
    assert [event["type"] for event in db.runs.list_events("s-live")] == [
        "message.delta",
        "message.delta",
    ]

    db.runs.append_event(
        "s-live",
        {
            "type": "message.complete",
            "run_id": "run-A",
            "turn_id": "turn-A",
            "seq": 3,
            "payload": {"status": "completed", "text": "hello"},
        },
    )

    assert [event["type"] for event in db.runs.list_events("s-live")] == ["message.complete"]
    archive = db._conn.execute(  # noqa: SLF001 - storage contract assertion.
        "SELECT reason, event_count, first_seq, last_seq FROM run_event_archives"
    ).fetchone()
    assert archive["reason"] == "terminal_run_stream_events"
    assert archive["event_count"] == 2
    assert archive["first_seq"] == 1
    assert archive["last_seq"] == 2


def test_append_run_event_does_not_special_case_retired_mirror_delta(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    payload = {
        "mode": "append",
        "text": "final report",
        "delta": "final report",
        "team_mission_conversation_mirror": True,
        "team_mission_final_deliverable": True,
        "mission_id": "mission-1",
        "source_run_id": "run-synthesis",
    }
    db.runs.append_event(
        "team-session-1",
        {
            "type": "message.delta",
            "run_id": "team-mission:mission-1:conversation:run-synthesis",
            "turn_id": "turn-synthesis",
            "seq": 1,
            "payload": payload,
        },
    )
    db.runs.append_event(
        "team-session-1",
        {
            "type": "message.complete",
            "run_id": "team-mission:mission-1:conversation:run-synthesis",
            "turn_id": "turn-synthesis",
            "seq": 2,
            "payload": {**payload, "status": "complete"},
        },
    )

    events = db.runs.list_events("team-session-1")
    archive = db._conn.execute(  # noqa: SLF001 - storage contract assertion.
        "SELECT reason, event_count, first_seq, last_seq FROM run_event_archives"
    ).fetchone()

    assert [event["type"] for event in events] == ["message.complete"]
    assert archive["reason"] == "terminal_run_stream_events"
    assert archive["event_count"] == 1


def test_reconcile_heals_stuck_running_regular_session_with_terminal_run(tmp_path: Path):
    """Pre-fix builds left regular sessions stuck at `running=1` after the
    streaming append_run_event projection gap. The boot-time reconcile sweeps
    those up: if the active_run_id is already terminal in the runs table, the
    session_index row gets reset to idle."""
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("s-stuck", source="cli")
    # Simulate the pre-fix stuck state: index says running=1 with an
    # active_run_id, but the run itself completed.
    db.session_index.upsert(
        session_id="s-stuck",
        source="cli",
        running=True,
        status="running",
        active_run_id="run-done",
        started_at=1.0,
        updated_at=1.0,
    )
    db.runs.upsert(run_id="run-done", session_id="s-stuck", status="completed")
    # Force the index back to the pre-fix stuck shape (upsert_run already
    # projected idle on the fix path; we simulate the legacy state by
    # re-running the stuck upsert).
    db.session_index.upsert(
        session_id="s-stuck",
        source="cli",
        running=True,
        status="running",
        active_run_id="run-done",
        started_at=1.0,
        updated_at=1.0,
    )
    row = db._conn.execute(  # noqa: SLF001 - storage contract assertion.
        "SELECT running, active_run_id FROM session_index WHERE session_id = ?",
        ("s-stuck",),
    ).fetchone()
    assert int(row["running"]) == 1
    assert row["active_run_id"] == "run-done"

    db.session_index.reconcile()

    item = db.session_index.list()["sessions"][0]
    assert item["running"] is False
    assert item["status"] == "idle"
    assert item["active_run_id"] == ""


def test_run_terminal_does_not_clobber_other_active_run(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.session_index.upsert(session_id="s1", source="cli", started_at=1.0, updated_at=1.0)
    db.runs.upsert(run_id="run-A", session_id="s1", status="running")
    # a different (stale) run terminating must not clear the active run-A
    db.runs.upsert(run_id="run-B", session_id="s1", status="completed")
    item = db.session_index.list()["sessions"][0]
    assert item["running"] is True
    assert item["active_run_id"] == "run-A"


def test_team_conversation_leader_terminal_clears_index_when_no_mission_is_active(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.upsert_team_mission_conversation(
        conversation_id="conv-chat",
        team_id="team-1",
        conversation_session_id="team-session-chat",
        title="团队会话",
    )
    db.runs.upsert(run_id="team-leader-run-1", session_id="team-session-chat", status="running")
    row = db._conn.execute(  # noqa: SLF001 - storage contract assertion.
        "SELECT running, active_run_id FROM session_index WHERE session_id = ?",
        ("team-session-chat",),
    ).fetchone()
    assert int(row["running"]) == 1
    assert row["active_run_id"] == "team-leader-run-1"

    db.runs.append_event(
        "team-session-chat",
        {
            "type": "message.complete",
            "run_id": "team-leader-run-1",
            "turn_id": "team-leader-turn-1",
            "seq": 2,
            "payload": {"status": "completed", "text": "done"},
        },
    )

    row = db._conn.execute(  # noqa: SLF001 - verifies write-time projection.
        "SELECT running, status, active_run_id FROM session_index WHERE session_id = ?",
        ("team-session-chat",),
    ).fetchone()
    assert int(row["running"]) == 0
    assert row["status"] == "idle"
    assert row["active_run_id"] == ""


def test_team_mission_active_run_terminal_does_not_clear_active_mission_index(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-live",
        team_id="team-1",
        title="Mission",
        mode="supervised_mission",
        status="running",
    )
    db.upsert_team_mission_conversation(
        conversation_id="conv-mission",
        team_id="team-1",
        conversation_session_id="team-session-mission",
        title="团队任务",
        active_mission_id="mission-live",
    )
    db.runs.upsert(run_id="team-leader-run-2", session_id="team-session-mission", status="running")

    db.runs.append_event(
        "team-session-mission",
        {
            "type": "message.complete",
            "run_id": "team-leader-run-2",
            "turn_id": "team-leader-turn-2",
            "seq": 2,
            "payload": {"status": "completed", "text": "task started"},
        },
    )

    row = db._conn.execute(  # noqa: SLF001 - verifies active mission is not clobbered.
        "SELECT running, status, active_run_id FROM session_index WHERE session_id = ?",
        ("team-session-mission",),
    ).fetchone()
    assert int(row["running"]) == 1
    assert row["status"] == "running"
    assert row["active_run_id"] == "team-leader-run-2"
    item = next(
        s for s in db.session_index.list()["sessions"]
        if s["session_id"] == "team-session-mission"
    )
    assert item["running"] is True
    assert item["active_run_id"] == "team-leader-run-2"


def test_team_conversation_projects_into_session_index(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.upsert_team_mission_conversation(
        conversation_id="conv-1",
        team_id="team-1",
        conversation_session_id="team-session-1",
        title="团队会话",
        active_mission_id="mission-1",
    )
    items = db.session_index.list()["sessions"]
    assert len(items) == 1
    it = items[0]
    assert it["session_id"] == "team-session-1"
    assert it["session_kind"] == "team_mission"
    assert it["team_id"] == "team-1"
    assert it["mission_id"] == "mission-1"
    assert it["title"] == "团队会话"


def test_update_session_index_for_mission_sets_running(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.upsert_team_mission_conversation(
        conversation_id="conv-1", team_id="team-1",
        conversation_session_id="team-session-1", title="t", active_mission_id="mission-1",
    )
    assert db.update_session_index_for_mission("mission-1", status="running", running=True) == 1
    it = db.session_index.list()["sessions"][0]
    assert it["running"] is True and it["status"] == "running"
    assert it["runtime_scope_key"] == "team:conv-1:leader-conversation"

    db.update_session_index_for_mission("mission-1", status="waiting_approval", running=False, waiting_approval=True)
    it = db.session_index.list()["sessions"][0]
    assert it["waiting_approval"] is True and it["status"] == "waiting_approval"


def test_team_conversation_projection_carries_canonical_runtime_scope(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")

    db.upsert_team_mission_conversation(
        conversation_id="conv-scope",
        team_id="team-1",
        conversation_session_id="team-session-scope",
        title="团队任务",
    )

    item = db.session_index.list()["sessions"][0]
    assert item["session_id"] == "team-session-scope"
    assert item["runtime_scope_key"] == "team:conv-scope:leader-conversation"


def test_list_session_index_repairs_active_team_runtime_identity_from_leader_run(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.upsert_team_mission_conversation(
        conversation_id="conv-repair",
        team_id="team-1",
        conversation_session_id="team-session-repair",
        title="团队任务",
        active_mission_id="mission-repair",
    )
    db.upsert_team_mission(
        mission_id="mission-repair",
        conversation_id="conv-repair",
        team_id="team-1",
        title="Mission",
        mode="supervised_mission",
        status="running",
        leader_session_id="team-session-repair",
    )
    db.runs.upsert(
        run_id="leader-run-repair",
        session_id="team-session-repair",
        runtime_scope_key="team:conv-repair:leader-conversation",
        execution_session_id="runtime-leader-repair",
        status="running",
    )
    db._conn.execute(  # noqa: SLF001 - simulate legacy production projection.
        """
        UPDATE session_index
           SET running = 1,
               status = 'running',
               runtime_scope_key = '',
               active_run_id = '',
               active_execution_session_id = ''
         WHERE session_id = ?
        """,
        ("team-session-repair",),
    )
    db._conn.commit()  # noqa: SLF001 - make the simulated stale row visible.

    db.session_index.reconcile()
    item = next(
        session for session in db.session_index.list()["sessions"]
        if session["session_id"] == "team-session-repair"
    )

    assert item["running"] is True
    assert item["runtime_scope_key"] == "team:conv-repair:leader-conversation"
    assert item["active_run_id"] == "leader-run-repair"
    assert item["active_execution_session_id"] == "runtime-leader-repair"


def test_team_conversation_touch_preserves_live_mission_status(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.upsert_team_mission_conversation(
        conversation_id="conv-1", team_id="team-1",
        conversation_session_id="team-session-1", title="t", active_mission_id="mission-1",
    )
    db.update_session_index_for_mission("mission-1", status="running", running=True)
    # a later conversation touch (e.g. title update) must NOT reset running
    db.upsert_team_mission_conversation(
        conversation_id="conv-1", team_id="team-1",
        conversation_session_id="team-session-1", title="renamed", active_mission_id="mission-1",
        replace_title=True,
    )
    it = db.session_index.list()["sessions"][0]
    assert it["title"] == "renamed"
    assert it["running"] is True
    assert it["status"] == "running"


def test_reconcile_excludes_team_internal_node_sessions(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("conv-1", source="cli")
    db.sessions.create("team:mission-abc:node:verify-x", source="cli")
    db.session_index.reconcile()
    ids = _ids(db.session_index.list())
    assert "conv-1" in ids
    assert "team:mission-abc:node:verify-x" not in ids


def test_reconcile_purges_previously_leaked_node_rows(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    # simulate a leaked node-session row from an earlier reconcile
    db.session_index.upsert(session_id="team:mission-abc:node:synthesis", source="cli", started_at=1.0, updated_at=1.0)
    assert "team:mission-abc:node:synthesis" in _ids(db.session_index.list())
    db.session_index.reconcile()
    assert "team:mission-abc:node:synthesis" not in _ids(db.session_index.list())


def test_reconcile_excludes_team_node_delegate_task_children(tmp_path: Path):
    """delegate_task spawns a fresh tui session whose parent_session_id points at
    the team node session. Such children must not surface in the sidebar — every
    team task that runs delegate_task would otherwise leak worker chatter as
    unattributed `tui` rows ('Get latest GitHub stats' / empty '新会话')."""
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("conv-1", source="cli")
    db.sessions.create("team:mission-xyz:node:tech-research", source="cli")
    db.sessions.create(
        "20260621_195327_cc3473",
        source="tui",
        parent_session_id="team:mission-xyz:node:tech-research",
    )
    db.session_index.reconcile()
    ids = _ids(db.session_index.list())
    assert "conv-1" in ids
    assert "team:mission-xyz:node:tech-research" not in ids
    assert "20260621_195327_cc3473" not in ids


def test_reconcile_purges_previously_leaked_delegate_task_children(tmp_path: Path):
    """A prior reconcile that ran before the parent-filter fix could have inserted
    delegate_task children into the index. Reconcile must clean them up too."""
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("team:mission-xyz:node:tech-research", source="cli")
    db.sessions.create(
        "20260621_195327_cc3473",
        source="tui",
        parent_session_id="team:mission-xyz:node:tech-research",
    )
    # simulate the leak from before the fix
    db.session_index.upsert(
        session_id="20260621_195327_cc3473", source="tui", started_at=1.0, updated_at=1.0,
    )
    assert "20260621_195327_cc3473" in _ids(db.session_index.list())
    db.session_index.reconcile()
    assert "20260621_195327_cc3473" not in _ids(db.session_index.list())


def test_reconcile_heals_terminal_mission_conversation_stuck_running(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.upsert_team_mission(mission_id="m-x", team_id="t", title="T", mode="supervised_mission", status="cancelled")
    db.upsert_team_mission_conversation(
        conversation_id="c-x", team_id="t", conversation_session_id="team-session-x",
        title="t", active_mission_id="m-x",
    )
    # simulate the stale running projection (cancel bypassed the reducer)
    db.update_session_index_for_mission("m-x", status="running", running=True)
    assert db.session_index.list()["sessions"][0]["running"] is True

    db.session_index.reconcile()

    item = db.session_index.list()["sessions"][0]
    assert item["running"] is False
    assert item["status"] == "idle"


def test_cancel_team_mission_sets_conversation_index_idle(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.upsert_team_mission(mission_id="m-y", team_id="t", title="T", mode="supervised_mission", status="running")
    db.upsert_team_mission_conversation(
        conversation_id="c-y", team_id="t", conversation_session_id="team-session-y",
        title="t", active_mission_id="m-y",
    )
    db.update_session_index_for_mission("m-y", status="running", running=True)
    assert db.session_index.list()["sessions"][0]["running"] is True

    db.cancel_team_mission(mission_id="m-y", canceled_by="user")

    item = db.session_index.list()["sessions"][0]
    assert item["running"] is False
    assert item["status"] == "idle"
    assert item["waiting_approval"] is False

    events = db.list_team_mission_events("m-y")
    assert any(
        (event.get("payload") or {}).get("source_event_type") == "mission.cancelled"
        for event in events
    )
    status_events = [event for event in events if event.get("type") == "team_mission.conversation.status"]
    assert status_events
    conversation = status_events[-1]["payload"]["projection"]
    assert conversation["mission_status"] == "cancelled"
    assert conversation["run_state"] == "cancelled"
    assert conversation["waiting_approval"] is False
    assert conversation["pending_approval_count"] == 0


def test_reject_team_mission_plan_clears_conversation_approval_projection(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.upsert_team_mission_conversation(
        conversation_id="c-r",
        team_id="t",
        conversation_session_id="team-session-r",
        title="t",
        active_mission_id="m-r",
    )
    db.upsert_team_mission(
        mission_id="m-r",
        conversation_id="c-r",
        team_id="t",
        title="T",
        mode="supervised_mission",
        status="waiting_approval",
        leader_session_id="team-session-r",
    )
    db.upsert_team_mission_node(
        mission_id="m-r",
        node_id="root",
        kind="root",
        title="Root",
        status="waiting_approval",
        runtime_scope_key="team:m-r:root",
        metadata={"task_id": "task-r"},
    )
    db.upsert_team_mission_node(
        mission_id="m-r",
        node_id="approval",
        kind="approval_gate",
        title="审批任务图",
        status="waiting_approval",
        runtime_scope_key="team:m-r:approval",
        metadata={"task_id": "task-r"},
    )
    db.update_session_index_for_mission("m-r", status="waiting_approval", running=False, waiting_approval=True)
    assert db.session_index.list()["sessions"][0]["waiting_approval"] is True

    result = db.reject_team_mission_plan(
        mission_id="m-r",
        task_id="task-r",
        rejected_by="user",
        reason="用户取消规划审批",
    )

    # 2026-07-06: reject plan == cancel mission,主表和 link 表都对齐 cancelled。
    # 之前写 "draft" 导致 desktop 判 mission 为非 terminal,审批卡反复出现。
    assert result["graph"]["mission"]["status"] == "cancelled"
    item = db.session_index.list()["sessions"][0]
    assert item["running"] is False
    assert item["status"] == "idle"
    assert item["waiting_approval"] is False
    assert item["pending_approval_count"] == 0

    events = db.list_team_mission_events("m-r")
    assert any(
        (event.get("payload") or {}).get("source_event_type") == "mission.plan.rejected"
        for event in events
    )
    status_events = [event for event in events if event.get("type") == "team_mission.conversation.status"]
    assert status_events
    conversation = status_events[-1]["payload"]["projection"]
    assert conversation["mission_status"] == "cancelled"
    assert conversation["run_state"] == "cancelled"
    assert conversation["waiting_approval"] is False
    assert conversation["pending_approval_count"] == 0


def test_reconcile_clears_stale_waiting_approval_for_rejected_draft_mission(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.upsert_team_mission_conversation(
        conversation_id="c-r2",
        team_id="t",
        conversation_session_id="team-session-r2",
        title="t",
        active_mission_id="m-r2",
    )
    db.upsert_team_mission(
        mission_id="m-r2",
        conversation_id="c-r2",
        team_id="t",
        title="T",
        mode="supervised_mission",
        status="draft",
        leader_session_id="team-session-r2",
    )
    db.upsert_team_mission_node(
        mission_id="m-r2",
        node_id="approval",
        kind="approval_gate",
        title="审批任务图",
        status="cancelled",
    )
    db.upsert_team_mission_node(
        mission_id="m-r2",
        node_id="worker",
        kind="worker",
        title="执行",
        status="cancelled",
    )
    db.update_session_index_for_mission("m-r2", status="waiting_approval", running=False, waiting_approval=True)
    assert db.session_index.list()["sessions"][0]["waiting_approval"] is True

    db.session_index.reconcile()

    item = db.session_index.list()["sessions"][0]
    assert item["running"] is False
    assert item["status"] == "idle"
    assert item["waiting_approval"] is False
    assert item["pending_approval_count"] == 0


def test_reconcile_clears_stale_direct_waiting_state_without_active_run(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create(session_id="direct-stale-waiting", source="tui")
    db.session_index.upsert(
        session_id="direct-stale-waiting",
        status="waiting_approval",
        running=False,
        waiting_approval=True,
        pending_approval_count=1,
        active_run_id="",
    )

    db.session_index.reconcile()

    item = db.session_index.list()["sessions"][0]
    assert item["status"] == "idle"
    assert item["running"] is False
    assert item["waiting_approval"] is False
    assert item["pending_approval_count"] == 0


def test_update_for_mission_not_running_clears_active_run_id(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.session_index.upsert(
        session_id="team-session-z", session_kind="team_mission", mission_id="m-z",
        running=True, status="running", active_run_id="team-leader-run-z",
        active_execution_session_id="rt-z", started_at=1.0, updated_at=1.0,
    )

    db.update_session_index_for_mission("m-z", status="idle", running=False)

    item = db.session_index.list()["sessions"][0]
    assert item["running"] is False
    # the sidebar treats a non-empty active_run_id as running — it MUST be cleared.
    assert item["active_run_id"] == ""
    assert item["active_execution_session_id"] == ""


def test_reconcile_clears_stale_active_run_on_already_idle_terminal_mission(tmp_path: Path):
    db = open_cli_session_store(tmp_path / "state.db")
    db.upsert_team_mission(mission_id="m-w", team_id="t", title="T", mode="supervised_mission", status="completed")
    db.upsert_team_mission_conversation(
        conversation_id="c-w", team_id="t", conversation_session_id="team-session-w",
        title="t", active_mission_id="m-w",
    )
    # running/status already healed to idle, but a stale active_run_id lingers —
    # the exact state that kept finished team conversations spinning after restart.
    db.session_index.upsert(
        session_id="team-session-w", session_kind="team_mission", mission_id="m-w",
        running=False, status="idle", active_run_id="team-leader-run-w",
        active_execution_session_id="rt-w", started_at=1.0, updated_at=1.0,
    )

    db.session_index.reconcile()

    item = db.session_index.list()["sessions"][0]
    assert item["active_run_id"] == ""
    assert item["active_execution_session_id"] == ""


def test_initialize_team_mission_lights_up_sidebar_immediately(tmp_path: Path):
    """Symptom A: starting a new team task left the sidebar idle the whole time
    the task was executing — running came back only at the synthesis stage.

    Root cause: initialize_team_mission_from_strategy creates the mission and
    nodes, but no projection sets session_index.running=1 for the *initial*
    planning state. The reducer only projects on a status CHANGE, and the
    leader root planning run uses a team:mission:node:root session id that does
    not match the conversation's session_index row, so the run-write projection
    updates a different row. The conversation's row therefore stays running=0.

    Fix: initialize_team_mission_from_strategy now explicitly projects the
    patch's mission_status onto the conversation's session_index row at the end
    of mission init."""
    db = open_cli_session_store(tmp_path / "state.db")
    db.upsert_team_mission_conversation(
        conversation_id="conv-a", team_id="t", conversation_session_id="team-session-a",
        title="t",
    )
    # Conversation row exists, running=0 by default.
    sessions = db.session_index.list()["sessions"]
    row = next(s for s in sessions if s["session_id"] == "team-session-a")
    assert row["running"] is False

    db.initialize_team_mission_from_strategy(
        mission_id="m-a",
        conversation_id="conv-a",
        team_id="t",
        title="Test",
        objective="o",
        mode="supervised_mission",
        leader_session_id="team-session-a",
        members=[],
    )

    sessions = db.session_index.list()["sessions"]
    row = next(s for s in sessions if s["session_id"] == "team-session-a")
    assert row["running"] is True, "sidebar must light up immediately when a team task starts"
    assert row["mission_id"] == "m-a"


def test_list_session_index_repairs_team_conversation_row_with_terminal_active_run(tmp_path: Path):
    """Symptom B: a team conversation whose leader run had completed cleanly
    still showed running=1 because the run-write projection was missed under
    some path.

    Team conversations use session_kind='team_mission' even when no active
    mission is bound. The list read path must repair old rows whose active run
    is already terminal so the sidebar does not keep spinning until restart."""
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("team-session-stuck", source="team_mission")
    # The run terminated cleanly in the runs table…
    db.runs.upsert(run_id="run-completed", session_id="team-session-stuck", status="completed")
    # …but the session_index row never got cleared (production reproduces this
    # when the run-write projection is bypassed on the leader's conversation
    # run ending — observed in the live DB). Re-set the stuck state AFTER the
    # run is terminal so the heal has real work to do.
    db.session_index.upsert(
        session_id="team-session-stuck",
        session_kind="team_mission",
        status="running",
        running=True,
        active_run_id="run-completed",
        active_execution_session_id="rt-x",
        mission_id="",
        conversation_id="conv-stuck",
        started_at=1.0,
        updated_at=1.0,
    )

    row = db._conn.execute(  # noqa: SLF001 - verify the legacy stuck state.
        "SELECT running, active_run_id FROM session_index WHERE session_id = ?",
        ("team-session-stuck",),
    ).fetchone()
    assert int(row["running"]) == 1
    assert row["active_run_id"] == "run-completed"

    db.session_index.reconcile()
    row = next(s for s in db.session_index.list()["sessions"] if s["session_id"] == "team-session-stuck")
    assert row["running"] is False
    assert row["active_run_id"] == ""
    assert row["status"] == "idle"
