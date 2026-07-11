from __future__ import annotations

from pathlib import Path

from hermes_agent.domain.member_chat_projection_service import MemberChatProjectionService
from hermes_agent.storage.cli_session_store import open_cli_session_store


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_member_chat_facade_module_is_deleted() -> None:
    assert not (
        REPO_ROOT
        / "hermes_agent"
        / "application"
        / "state_facade"
        / "member_chat_facade.py"
    ).exists()


def test_legacy_state_store_is_deleted() -> None:
    assert not (REPO_ROOT / "hermes_agent" / "storage" / "state_store.py").exists()


def test_cli_store_owns_member_chat_projection_through_domain_service(
    tmp_path: Path,
) -> None:
    store = open_cli_session_store(tmp_path / "state.db")
    try:
        assert isinstance(store.member_chat_views, MemberChatProjectionService)
        assert not hasattr(store, "recall_member_chat_view_messages")
        assert not hasattr(store, "sync_member_chat_conversation_view")
    finally:
        store.close()
