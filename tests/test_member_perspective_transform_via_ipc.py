from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from hermes_state import SessionDB
from hermes_team_mission.state.session_views import transform_to_member_perspective
from tui_gateway.run_worker import DBRpcRequestFrame
from hermes_agent.orchestration.worker_db_proxy import WorkerDBProxy
from tui_gateway.services.worker_supervisor import WorkerSupervisor


CONVERSATION_SESSION_ID = "team-session-ipc"
LEADER_PARTICIPANT_ID = "leader:conv-ipc"
ALICE_PARTICIPANT_ID = "member:alice"
BOB_PARTICIPANT_ID = "member:bob"


class _SupervisorIPCWriter:
    """In-memory stand-in for the worker stdout -> main supervisor IPC hop."""

    def __init__(self, supervisor: WorkerSupervisor) -> None:
        self._supervisor = supervisor
        self.proxy: WorkerDBProxy | None = None

    def write_json(self, obj: dict[str, Any]) -> None:
        if self.proxy is None:
            raise RuntimeError("proxy not attached")
        reply = asyncio.run(
            self._supervisor._execute_db_rpc(
                DBRpcRequestFrame(
                    id=str(obj["id"]),
                    method=str(obj["method"]),
                    params=obj.get("params"),
                    db_scope=obj.get("db_scope") or {},
                )
            )
        )
        payload = {"jsonrpc": "2.0", "id": reply.id}
        if reply.error is not None:
            payload["error"] = reply.error
        else:
            payload["result"] = reply.result
        self.proxy.handle_reply(payload)


@pytest.fixture
def team_db(tmp_path: Path) -> SessionDB:
    db = SessionDB(tmp_path / "state.db")
    db.create_session(CONVERSATION_SESSION_ID, source="team_mission")
    db.upsert_conversation_participant(
        conversation_session_id=CONVERSATION_SESSION_ID,
        participant_id=LEADER_PARTICIPANT_ID,
        role="leader",
        display_name="Leader Name",
        runtime_scope_key="profile:leader",
    )
    db.upsert_conversation_participant(
        conversation_session_id=CONVERSATION_SESSION_ID,
        participant_id=ALICE_PARTICIPANT_ID,
        role="member",
        display_name="Alice",
        runtime_scope_key="profile:alice",
    )
    db.upsert_conversation_participant(
        conversation_session_id=CONVERSATION_SESSION_ID,
        participant_id=BOB_PARTICIPANT_ID,
        role="member",
        display_name="Bob",
        runtime_scope_key="profile:bob",
    )
    db.append_message(
        CONVERSATION_SESSION_ID,
        role="user",
        content="@Alice please review the plan.",
        metadata={"participant_id": "user:requester", "display_name": "Requester"},
    )
    db.append_message(
        CONVERSATION_SESSION_ID,
        role="assistant",
        content="I will coordinate this review.",
        metadata={"participant_id": LEADER_PARTICIPANT_ID},
    )
    db.append_message(
        CONVERSATION_SESSION_ID,
        role="assistant",
        content="Alice prior response should stay assistant.",
        metadata={"participant_id": ALICE_PARTICIPANT_ID},
    )
    db.append_message(
        CONVERSATION_SESSION_ID,
        role="assistant",
        content="Bob prior response should become observed speech.",
        metadata={"participant_id": BOB_PARTICIPANT_ID},
    )
    return db


@pytest.fixture
def worker_db_proxy(
    team_db: SessionDB,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    from tui_gateway import server as _server

    monkeypatch.setattr(_server, "_db_for_stable_session", lambda _stable: team_db)
    supervisor = WorkerSupervisor(
        on_event=_noop,
        on_interactive_request=_noop,
        on_run_terminal=_noop,
    )
    writer = _SupervisorIPCWriter(supervisor)
    proxy = WorkerDBProxy(writer, timeout_s=1)
    writer.proxy = proxy
    return proxy.scoped(CONVERSATION_SESSION_ID)


def test_get_messages_via_worker_db_proxy_returns_same_data_as_direct(
    team_db: SessionDB,
    worker_db_proxy: Any,
) -> None:
    direct = team_db.get_messages_as_conversation(CONVERSATION_SESSION_ID)

    via_ipc = worker_db_proxy.get_messages_as_conversation(CONVERSATION_SESSION_ID)

    assert via_ipc == direct


def test_list_participants_via_worker_db_proxy_returns_same_data(
    team_db: SessionDB,
    worker_db_proxy: Any,
) -> None:
    direct = team_db.list_conversation_participants(CONVERSATION_SESSION_ID)

    via_ipc = worker_db_proxy.list_conversation_participants(CONVERSATION_SESSION_ID)

    assert via_ipc == direct


def test_transform_to_member_perspective_produces_same_output_under_ipc(
    team_db: SessionDB,
    worker_db_proxy: Any,
) -> None:
    direct_messages = team_db.get_messages_as_conversation(CONVERSATION_SESSION_ID)
    direct_participants = team_db.list_conversation_participants(CONVERSATION_SESSION_ID)
    direct_projection = transform_to_member_perspective(
        direct_messages,
        viewing_participant_id=ALICE_PARTICIPANT_ID,
        participants=direct_participants,
    )

    ipc_messages = worker_db_proxy.get_messages_as_conversation(CONVERSATION_SESSION_ID)
    ipc_participants = worker_db_proxy.list_conversation_participants(CONVERSATION_SESSION_ID)
    ipc_projection = transform_to_member_perspective(
        ipc_messages,
        viewing_participant_id=ALICE_PARTICIPANT_ID,
        participants=ipc_participants,
    )

    assert ipc_projection == direct_projection
    assert [message["role"] for message in ipc_projection] == [
        "system",
        "user",
        "user",
        "assistant",
        "user",
    ]
    assert ipc_projection[0]["metadata"]["team_member_identity_contract"] is True
    assert ipc_projection[2]["content"] == "[Leader Name] I will coordinate this review."
    assert ipc_projection[3]["content"] == "Alice prior response should stay assistant."
    assert (
        ipc_projection[4]["content"]
        == "[Bob] Bob prior response should become observed speech."
    )


async def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None
