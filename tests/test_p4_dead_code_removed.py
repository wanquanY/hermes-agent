from __future__ import annotations

import inspect
import logging
from pathlib import Path

from hermes_agent.application.session_index_service import SessionIndexService
from hermes_agent.composition.cli_session_store import open_cli_session_store


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_session_index_active_mission_id_no_longer_written(tmp_path: Path) -> None:
    db = open_cli_session_store(tmp_path / "state.db")

    session_index_columns = {
        row["name"]
        for row in db._conn.execute("PRAGMA table_info(session_index)").fetchall()  # noqa: SLF001
    }
    assert "active_mission_id" not in session_index_columns

    conversation_columns = {
        row["name"]
        for row in db._conn.execute(  # noqa: SLF001
            "PRAGMA table_info(team_mission_conversations)"
        ).fetchall()
    }
    assert "active_mission_id" in conversation_columns

    conversation = db.upsert_team_mission_conversation(
        conversation_id="conv-p4",
        conversation_session_id="session-p4",
        title="P4",
        active_mission_id="mission-p4",
    )

    assert conversation["active_mission_id"] == "mission-p4"
    assert [row["mission_id"] for row in db.list_conversation_missions("conv-p4")] == ["mission-p4"]
    raw = db._conn.execute(  # noqa: SLF001
        "SELECT active_mission_id FROM team_mission_conversations WHERE conversation_id = ?",
        ("conv-p4",),
    ).fetchone()
    assert (raw["active_mission_id"] or "") == ""


def test_upsert_session_index_signature_does_not_require_active_mission(tmp_path: Path) -> None:
    signature = inspect.signature(SessionIndexService.upsert)
    assert "active_mission_id" not in signature.parameters

    db = open_cli_session_store(tmp_path / "state.db")
    db.session_index.upsert(session_id="session-p4", started_at=1.0, updated_at=2.0)

    row = db.session_index.get("session-p4")
    assert row is not None
    assert row["session_id"] == "session-p4"
    assert row["mission_id"] == ""


def test_list_team_mission_run_events_removed_or_warned(tmp_path: Path, caplog) -> None:
    db = open_cli_session_store(tmp_path / "state.db")
    alias = getattr(db, "list_team_mission_run_events", None)
    if alias is None:
        return

    caplog.set_level(logging.WARNING)
    assert alias("mission-p4") == []
    assert "list_team_mission_run_events is deprecated audit compatibility" in caplog.text


def test_active_mission_id_write_sql_removed() -> None:
    assert not (REPO_ROOT / "hermes_state.py").exists()
    sources = [
        REPO_ROOT / "hermes_team_mission" / "state" / "conversation_missions.py",
        REPO_ROOT / "hermes_team_mission" / "state" / "session_conversations.py",
    ]
    joined = "\n".join(path.read_text(encoding="utf-8") for path in sources)

    assert "SET active_mission_id" not in joined
    assert "active_mission_id = COALESCE" not in joined
    assert "_sync_legacy_active_mission_id_on_conn" not in joined
