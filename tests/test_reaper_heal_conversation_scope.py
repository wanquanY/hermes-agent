from __future__ import annotations

from pathlib import Path

from hermes_agent.storage.cli_session_store import CliSessionStore, open_cli_session_store


def _db(tmp_path: Path) -> CliSessionStore:
    return open_cli_session_store(tmp_path / "state.db")


def _row(db: CliSessionStore, session_id: str) -> dict:
    row = db._conn.execute(  # noqa: SLF001 - storage-level heal contract.
        "SELECT * FROM session_index WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    assert row is not None
    return dict(row)


def _force_running_index(
    db: CliSessionStore,
    *,
    session_id: str,
    active_run_id: str,
    source: str = "tui",
    session_kind: str = "hermes_session",
    conversation_id: str = "",
    mission_id: str = "",
) -> None:
    db.session_index.upsert(
        session_id=session_id,
        title=f"Conversation {session_id}",
        preview="preview",
        source=source,
        session_kind=session_kind,
        conversation_id=conversation_id,
        mission_id=mission_id,
        running=True,
        status="running",
        active_run_id=active_run_id,
        active_execution_session_id=f"rt-{active_run_id}",
        pending_approval_count=1,
        started_at=1.0,
        updated_at=2.0,
    )


def _assert_idle(row: dict) -> None:
    assert row["running"] == 0
    assert row["status"] == "idle"
    assert row["waiting_approval"] == 0
    assert row["active_run_id"] == ""
    assert row["active_execution_session_id"] == ""
    assert row["pending_approval_count"] == 0


def test_heal_clears_conversation_with_no_active_run(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.runs.upsert(run_id="run-terminal", session_id="conv-1", status="completed")
    _force_running_index(db, session_id="conv-1", active_run_id="run-terminal")

    db.session_index.reconcile()

    _assert_idle(_row(db, "conv-1"))


def test_heal_does_not_clear_conversation_with_active_member_chat_run(tmp_path: Path) -> None:
    db = _db(tmp_path)
    session_id = "memberchat:worker-1"
    db.runs.upsert(run_id="run-terminal", session_id=session_id, status="completed")
    db.runs.upsert(run_id="run-member-chat", session_id=session_id, status="running")
    _force_running_index(db, session_id=session_id, active_run_id="run-terminal")

    db.session_index.reconcile()

    row = _row(db, session_id)
    assert row["running"] == 1
    assert row["status"] == "running"
    assert row["active_run_id"] == "run-terminal"


def test_heal_does_not_clear_conversation_with_mission_terminal_but_chat_run_active(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    db.upsert_team_mission(
        mission_id="mission-terminal",
        conversation_id="conv-team",
        team_id="team-1",
        title="Mission",
        status="cancelled",
    )
    db.runs.upsert(run_id="run-terminal", session_id="team-session", status="completed")
    db.runs.upsert(run_id="run-member-chat", session_id="team-session", status="running")
    _force_running_index(
        db,
        session_id="team-session",
        active_run_id="run-terminal",
        source="team_mission",
        session_kind="team_mission",
        conversation_id="conv-team",
        mission_id="mission-terminal",
    )

    db.session_index.reconcile()

    row = _row(db, "team-session")
    assert row["running"] == 1
    assert row["status"] == "running"
    assert row["active_run_id"] == "run-terminal"


def test_heal_handles_team_mission_run_correctly(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.upsert_team_mission(
        mission_id="mission-active",
        conversation_id="conv-team",
        team_id="team-1",
        title="Mission",
        status="running",
    )
    for node_id in ("node-a", "node-b"):
        run_id = f"run-{node_id}"
        session_id = f"team:mission-active:node:{node_id}"
        db.upsert_team_mission_node(
            mission_id="mission-active",
            node_id=node_id,
            kind="worker",
            title=node_id,
            status="completed",
        )
        db.runs.upsert(run_id=run_id, session_id=session_id, status="completed")
        db.bind_team_mission_run(
            mission_id="mission-active",
            node_id=node_id,
            run_id=run_id,
            session_id=session_id,
            role="worker",
        )
    _force_running_index(
        db,
        session_id="team-session",
        active_run_id="run-node-a",
        source="team_mission",
        session_kind="team_mission",
        conversation_id="conv-team",
        mission_id="mission-active",
    )

    db.session_index.reconcile()

    active_row = _row(db, "team-session")
    assert active_row["running"] == 1
    assert active_row["status"] == "running"

    db.upsert_team_mission(
        mission_id="mission-active",
        conversation_id="conv-team",
        team_id="team-1",
        title="Mission",
        status="cancelled",
    )
    db.session_index.reconcile()

    _assert_idle(_row(db, "team-session"))


def test_no_memberchat_prefix_purge_hack(tmp_path: Path) -> None:
    db = _db(tmp_path)
    session_id = "memberchat:first-class"
    db.sessions.create(session_id, source="team_mission")
    db.messages.append(session_id=session_id, role="user", content="hello")

    db.session_index.reconcile()

    row = _row(db, session_id)
    assert row["session_id"] == session_id
    assert row["source"] == "team_mission"
    assert row["message_count"] == 1


def test_legacy_session_index_row_not_corrupted_by_new_heal(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.session_index.upsert(
        session_id="legacy-row",
        title="Legacy",
        preview="keep me",
        source="cli",
        status="idle",
        running=False,
        active_run_id="",
        started_at=10.0,
        updated_at=11.0,
        message_count=7,
    )

    db.session_index.reconcile()

    row = _row(db, "legacy-row")
    assert row["title"] == "Legacy"
    assert row["preview"] == "keep me"
    assert row["source"] == "cli"
    assert row["message_count"] == 7
    _assert_idle(row)
