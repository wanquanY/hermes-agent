import threading
from types import SimpleNamespace

import pytest

from tui_gateway import server
from tui_gateway.methods import run as run_methods
from tui_gateway.services.pending_prompt_queue import (
    PendingPrompt,
    PendingPromptQueue,
    pending_prompt_queue,
)


@pytest.fixture(autouse=True)
def _clean_pending_queue():
    pending_prompt_queue.reset_for_tests()
    yield
    pending_prompt_queue.reset_for_tests()


def _prompt(run_id: str, *, text: str, attachments=None, transport=None) -> PendingPrompt:
    return PendingPrompt.from_submit(
        request_id=f"request-{run_id}",
        conversation_session_id="conversation-1",
        run_id=run_id,
        turn_id=f"turn-{run_id}",
        params={
            "text": text,
            "attachments": list(attachments or []),
            "model": "profile-model",
            "model_descriptor": {"provider": "codex"},
        },
        transport=transport,
        profile_context={"hermes_home": "/profiles/p1"},
        blocked_by_run_id="active-run",
    )


def test_pending_prompt_queue_preserves_typed_fifo_entries():
    queue = PendingPromptQueue()
    first_transport = object()
    first = _prompt(
        "run-1",
        text="inspect this",
        attachments=[{"path": "/tmp/image.png", "mimeType": "image/png"}],
        transport=first_transport,
    )
    second = _prompt("run-2", text="then summarize")

    assert queue.enqueue("profile-db", first) == 1
    assert queue.enqueue("profile-db", second) == 2
    assert queue.enqueue("profile-db", first) == 1

    snapshot = queue.snapshot("profile-db", "conversation-1")
    assert [item["run_id"] for item in snapshot] == ["run-1", "run-2"]
    assert snapshot[0]["attachments"] == [
        {"path": "/tmp/image.png", "mimeType": "image/png"}
    ]
    claimed = queue.claim_next("profile-db", "conversation-1")
    assert claimed is first
    assert claimed.transport is first_transport
    assert claimed.params["model_descriptor"] == {"provider": "codex"}


def test_run_submit_busy_queues_complete_request(monkeypatch):
    fake_db = SimpleNamespace(db_path="/profiles/p1/state.db")
    transport = object()
    session = {
        "agent": SimpleNamespace(),
        "history_lock": threading.Lock(),
        "profile_context": {"hermes_home": "/profiles/p1"},
        "running": True,
        "session_key": "conversation-1",
        "transport": transport,
    }
    server._sessions["runtime-1"] = session
    monkeypatch.setattr(run_methods, "_run_db_for_stable_session", lambda _sid: fake_db)
    monkeypatch.setattr(run_methods, "_load_busy_input_mode", lambda: "queue")
    monkeypatch.setattr(
        run_methods.run_control,
        "create_run_if_session_idle",
        lambda **_kwargs: {
            "run": None,
            "created": False,
            "conflict": {
                "run_id": "active-run",
                "turn_id": "active-turn",
                "status": "running",
                "execution_session_id": "runtime-1",
            },
        },
    )

    try:
        response = server._methods["run.submit"](
            "request-2",
            {
                "conversation_session_id": "conversation-1",
                "client_run_id": "run-2",
                "turn_id": "turn-2",
                "text": "next request",
                "attachments": [
                    {"path": "/tmp/native.png", "mimeType": "image/png"}
                ],
                "model": "profile-model",
                "model_descriptor": {"provider": "codex"},
            },
        )
    finally:
        server._sessions.pop("runtime-1", None)

    assert response["result"]["status"] == "queued"
    assert response["result"]["run_id"] == "run-2"
    queued = pending_prompt_queue.peek(
        "/profiles/p1/state.db",
        "conversation-1",
    )
    assert queued is not None
    assert queued.run_id == "run-2"
    assert queued.turn_id == "turn-2"
    assert queued.transport is transport
    assert queued.params["attachments"] == [
        {"path": "/tmp/native.png", "mimeType": "image/png"}
    ]
    assert queued.params["model_descriptor"] == {"provider": "codex"}


def test_drain_reenters_run_submit_with_original_identity(monkeypatch):
    fake_db = SimpleNamespace(db_path="/profiles/p1/state.db")
    prompt = _prompt(
        "run-2",
        text="next request",
        attachments=[{"path": "/tmp/native.png", "mimeType": "image/png"}],
    )
    pending_prompt_queue.enqueue(fake_db.db_path, prompt)
    dispatched = {}
    done = threading.Event()

    monkeypatch.setattr(
        run_methods.run_control,
        "session_status",
        lambda *_args, **_kwargs: {"running": False},
    )

    def _submit(rid, params):
        dispatched.update(rid=rid, params=params)
        done.set()
        return {"result": {"status": "streaming"}}

    monkeypatch.setitem(server._methods, "run.submit", _submit)

    assert run_methods.schedule_pending_prompt_drain(
        "conversation-1",
        db=fake_db,
    ) is True
    assert done.wait(2)
    assert dispatched["rid"] == "request-run-2"
    assert dispatched["params"]["client_run_id"] == "run-2"
    assert dispatched["params"]["turn_id"] == "turn-run-2"
    assert dispatched["params"]["attachments"] == [
        {"path": "/tmp/native.png", "mimeType": "image/png"}
    ]
    assert pending_prompt_queue.peek(fake_db.db_path, "conversation-1") is None


def test_drain_does_not_claim_while_control_plane_is_still_busy(monkeypatch):
    fake_db = SimpleNamespace(db_path="/profiles/p1/state.db")
    pending_prompt_queue.enqueue(fake_db.db_path, _prompt("run-2", text="later"))
    monkeypatch.setattr(
        run_methods.run_control,
        "session_status",
        lambda *_args, **_kwargs: {"running": True},
    )

    assert run_methods.schedule_pending_prompt_drain(
        "conversation-1",
        db=fake_db,
    ) is False
    assert pending_prompt_queue.peek(fake_db.db_path, "conversation-1") is not None
