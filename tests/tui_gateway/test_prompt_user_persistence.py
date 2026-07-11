from __future__ import annotations

from pathlib import Path

from hermes_agent.storage.cli_session_store import open_cli_session_store
from tui_gateway.methods import prompt


def _persist(
    monkeypatch,
    tmp_path: Path,
    *,
    text: str = "",
    persist_user_message: str = "",
    attachments: list[dict] | None = None,
):
    db = open_cli_session_store(tmp_path / "state.db")
    db.sessions.create("conversation-1", source="tui", transient=False)
    monkeypatch.setattr(prompt, "_db_for_stable_session", lambda _session_id: db)
    options = {
        "sid": "runtime-1",
        "session": {"transient": False},
        "conversation_session_id": "conversation-1",
        "runtime_scope_key": "profile:agent-default",
        "run_id": "run-1",
        "turn_id": "turn-1",
        "client_message_id": "client-1",
        "text": text,
        "persist_user_message": persist_user_message,
        "attachments": attachments or [],
        "draft_text": text,
        "model": "model-1",
        "model_descriptor": {"id": "model-1"},
        "dovie_product_context": "",
    }
    prompt._persist_prompt_user_turn(**options)
    return db, options


def test_prompt_user_persistence_preserves_original_whitespace(
    monkeypatch,
    tmp_path: Path,
) -> None:
    content = "  first line\n    indented code\n"
    db, _options = _persist(monkeypatch, tmp_path, text=content)

    [row] = db.messages.list("conversation-1")
    assert row["content"] == content


def test_prompt_user_persistence_records_attachment_only_turn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    attachment = {
        "name": "spec.pdf",
        "path": "/tmp/spec.pdf",
        "kind": "file",
    }
    db, _options = _persist(
        monkeypatch,
        tmp_path,
        attachments=[attachment],
    )

    [row] = db.messages.list("conversation-1")
    assert row["content"] == ""
    assert row["metadata"]["attachments"] == [attachment]
    assert row["metadata"]["client_message_id"] == "client-1"


def test_prompt_user_persistence_is_idempotent_for_same_turn(
    monkeypatch,
    tmp_path: Path,
) -> None:
    db, options = _persist(monkeypatch, tmp_path, text="hello")

    prompt._persist_prompt_user_turn(**options)

    rows = db.messages.list("conversation-1")
    assert len(rows) == 1
    assert rows[0]["metadata"]["persist_message_key"] == (
        "run:run-1|turn:turn-1|idx:0"
    )
