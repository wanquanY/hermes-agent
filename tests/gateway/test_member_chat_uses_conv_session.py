from __future__ import annotations

from pathlib import Path

from hermes_state import SessionDB
from hermes_team_mission.gateway import runtime_methods


CONVERSATION_ID = "conv-1"
CONVERSATION_SESSION_ID = "team-session-1"
TARGET_MEMBER_ID = "member-alice"


def _member_mission(tmp_path: Path) -> dict:
    return {
        "mission_id": "mission-1",
        "team_id": "team-1",
        "workspace_path": str(tmp_path),
        "metadata": {
            "members": [
                {
                    "member_id": TARGET_MEMBER_ID,
                    "agent_profile_id": "profile-alice",
                    "agent_profile_version_id": "version-alice",
                    "role": "member",
                    "profile_name": "Alice",
                    "dovie_profile": {
                        "id": "profile-alice",
                        "agentProfileVersionId": "version-alice",
                        "hermesHomePath": str(tmp_path / "alice-home"),
                    },
                }
            ]
        },
    }


def _submit_member(
    monkeypatch,
    tmp_path: Path,
    *,
    text: str = "@Alice please review this",
    extra_params: dict | None = None,
) -> tuple[SessionDB, dict, dict]:
    db = SessionDB(tmp_path / "state.db")
    captured: dict = {}

    def fake_proxy_run_submit(params: dict) -> dict:
        captured.update(params)
        return {"ok": True}

    monkeypatch.setattr(runtime_methods, "_proxy_run_submit_via_worker", fake_proxy_run_submit)
    params = {
        "team_id": "team-1",
        "conversation_id": CONVERSATION_ID,
        "conversation_session_id": CONVERSATION_SESSION_ID,
        "client_run_id": "optimistic-run-1",
        "turn_id": "turn-1",
        "cwd": str(tmp_path),
        "workspace": {"id": "workspace-1", "path": str(tmp_path), "kind": "local"},
    }
    if extra_params:
        params.update(extra_params)

    response = runtime_methods._submit_message_to_member(
        "rid-member",
        params,
        db=db,
        target_member_id=TARGET_MEMBER_ID,
        conversation_id=CONVERSATION_ID,
        conversation_session_id=CONVERSATION_SESSION_ID,
        mission=_member_mission(tmp_path),
        text=text,
    )

    assert "error" not in response, response
    assert captured
    return db, captured, response


def _memberchat_session_ids(db: SessionDB) -> list[str]:
    rows = db._conn.execute(  # noqa: SLF001 - test introspection
        "SELECT id FROM sessions WHERE id LIKE 'memberchat:%' ORDER BY id"
    ).fetchall()
    return [str(row["id"]) for row in rows]


def _table_exists(db: SessionDB, table_name: str) -> bool:
    row = db._conn.execute(  # noqa: SLF001 - test introspection
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def test_submit_does_not_create_memberchat_session(monkeypatch, tmp_path: Path):
    db, _captured, _response = _submit_member(monkeypatch, tmp_path)

    assert _memberchat_session_ids(db) == []


def test_submit_does_not_create_member_chat_runs_table(monkeypatch, tmp_path: Path):
    db, _captured, _response = _submit_member(monkeypatch, tmp_path)

    assert not _table_exists(db, "member_chat_runs")


def test_worker_spawn_uses_conversation_session_id(monkeypatch, tmp_path: Path):
    _db, captured, response = _submit_member(monkeypatch, tmp_path)

    assert captured["stored_session_id"] == CONVERSATION_SESSION_ID
    assert captured["session_id"] == CONVERSATION_SESSION_ID
    assert not captured["stored_session_id"].startswith("memberchat:")
    member_turn = response["result"]["member_turn"]
    assert member_turn["stored_session_id"] == CONVERSATION_SESSION_ID
    assert member_turn["worker_stored_session_id"] == CONVERSATION_SESSION_ID


def test_user_message_persists_to_conv_messages(monkeypatch, tmp_path: Path):
    db, _captured, _response = _submit_member(monkeypatch, tmp_path)

    messages = db.get_messages(CONVERSATION_SESSION_ID)
    user_messages = [msg for msg in messages if msg.get("role") == "user"]
    assert [msg.get("content") for msg in user_messages] == ["@Alice please review this"]
    metadata = user_messages[0].get("metadata") or {}
    assert metadata["team_mission"]["kind"] == "member_chat_user"
    assert metadata["team_mission"]["target_member_id"] == TARGET_MEMBER_ID


def test_user_message_persists_attachment_metadata_and_forwards_to_worker(
    monkeypatch,
    tmp_path: Path,
):
    attachments = [
        {
            "id": "image-1",
            "name": "architecture.png",
            "fileName": "architecture.png",
            "mimeType": "image/png",
            "size": 2048,
            "path": "/tmp/architecture.png",
            "kind": "image",
        }
    ]
    db, captured, _response = _submit_member(
        monkeypatch,
        tmp_path,
        text="@Alice please review this\n\n[Attachment Context]\n- architecture.png",
        extra_params={
            "draft_text": "@Alice please review this",
            "attachments": attachments,
        },
    )

    messages = db.get_messages(CONVERSATION_SESSION_ID)
    user_messages = [msg for msg in messages if msg.get("role") == "user"]
    metadata = user_messages[0].get("metadata") or {}

    assert user_messages[0]["content"] == "@Alice please review this"
    assert metadata["draft_text"] == "@Alice please review this"
    assert metadata["attachments"] == attachments
    assert metadata["attachment_count"] == 1
    assert captured["text"] == "@Alice please review this\n\n[Attachment Context]\n- architecture.png"
    assert captured["draft_text"] == "@Alice please review this"
    assert captured["attachments"] == attachments
