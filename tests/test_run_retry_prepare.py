from __future__ import annotations

from types import SimpleNamespace

import pytest

from tui_gateway import server
from tui_gateway.methods import run as run_methods


class _Messages:
    def __init__(self, messages: list[dict]) -> None:
        self._messages = messages

    def list(self, _conversation_session_id: str) -> list[dict]:
        return list(self._messages)


def _source_run(*, status: str = "failed", attempt: int = 1) -> dict:
    return {
        "run_id": "run-source",
        "conversation_session_id": "conversation-1",
        "runtime_scope_key": "profile:researcher",
        "status": status,
        "metadata": {
            "retry_attempt": attempt,
            "run_context_json": '{"conversation_session_id":"conversation-1"}',
            "activity_id": "act-chat:conversation-1",
            "activity_kind": "chat",
            "participant_id": "participant:researcher",
        },
    }


def _source_message() -> dict:
    return {
        "role": "user",
        "content": "Investigate the incident",
        "metadata": {
            "run_id": "run-source",
            "turn_id": "turn-source",
            "attachments": [{"path": "brief.md", "name": "brief.md"}],
            "draft_text": "Investigate the incident",
            "model": "model-a",
            "model_descriptor": {"id": "model-a"},
            "dovie_product_context": {"source": "desktop"},
        },
    }


@pytest.fixture
def retry_runtime(monkeypatch: pytest.MonkeyPatch):
    db = SimpleNamespace(messages=_Messages([_source_message()]))
    source = _source_run()
    monkeypatch.setattr(run_methods, "_run_db_for_stable_session", lambda _session_id: db)
    monkeypatch.setattr(
        run_methods.run_control,
        "get_run",
        lambda run_id, db=None: source if run_id == "run-source" else None,
    )
    return db, source


def _params(**overrides) -> dict:
    return {
        "conversation_session_id": "conversation-1",
        "source_run_id": "run-source",
        "client_run_id": "run-retry-2",
        "idempotency_key": "command-retry-2",
        "expected_attempt": 1,
        "turn_id": "turn-retry-2",
        **overrides,
    }


def test_run_retry_prepare_returns_owner_derived_submit_payload(retry_runtime) -> None:
    response = server._methods["run.retry.prepare"](7, _params())
    result = response["result"]
    submit = result["submit"]

    assert result["status"] == "prepared"
    assert result["source_attempt"] == 1
    assert result["retry_attempt"] == 2
    assert submit["conversation_session_id"] == "conversation-1"
    assert submit["client_run_id"] == "run-retry-2"
    assert submit["retry_of_run_id"] == "run-source"
    assert submit["text"] == "Investigate the incident"
    assert submit["runtime_scope_key"] == "profile:researcher"
    assert submit["run_context_json"] == '{"conversation_session_id":"conversation-1"}'
    assert submit["attachments"] == [{"path": "brief.md", "name": "brief.md"}]
    assert submit["model_descriptor"] == {"id": "model-a"}


def test_run_retry_prepare_rejects_attempt_conflict(retry_runtime) -> None:
    response = server._methods["run.retry.prepare"](8, _params(expected_attempt=2))

    assert response["error"]["code"] == 4409
    assert response["error"]["message"] == "retry attempt conflict"


def test_run_retry_prepare_rejects_non_terminal_source(retry_runtime) -> None:
    _db, source = retry_runtime
    source["status"] = "running"
    response = server._methods["run.retry.prepare"](9, _params())

    assert response["error"]["code"] == 4009
    assert "not retryable" in response["error"]["message"]


def test_run_retry_prepare_rejects_missing_source_message(retry_runtime) -> None:
    db, _source = retry_runtime
    db.messages = _Messages([])
    response = server._methods["run.retry.prepare"](10, _params())

    assert response["error"]["code"] == 4404
    assert response["error"]["message"] == "source user message not found"


def test_run_retry_prepare_uses_source_attempt_for_chained_retry(retry_runtime) -> None:
    _db, source = retry_runtime
    source["metadata"]["retry_attempt"] = 3
    response = server._methods["run.retry.prepare"](11, _params(expected_attempt=3))

    assert response["result"]["retry_attempt"] == 4
    assert response["result"]["submit"]["retry_attempt"] == 4


def test_run_intent_metadata_preserves_only_retry_execution_context() -> None:
    metadata = run_methods._run_intent_metadata(
        {
            "idempotency_key": "command-retry-2",
            "retry_of_run_id": "run-source",
            "retry_attempt": "2",
            "run_context_json": {"conversation_session_id": "conversation-1"},
            "participant_id": "participant:researcher",
            "activity_id": "act-chat:conversation-1",
            "activity_kind": "chat",
            "runtimeScopeKey": "profile:researcher",
            "text": "must not be duplicated into run metadata",
            "gateway_pid": "must not be caller-controlled",
        }
    )

    assert metadata == {
        "idempotency_key": "command-retry-2",
        "retry_of_run_id": "run-source",
        "retry_attempt": 2,
        "run_context_json": {"conversation_session_id": "conversation-1"},
        "participant_id": "participant:researcher",
        "activity_id": "act-chat:conversation-1",
        "activity_kind": "chat",
        "runtime_scope_key": "profile:researcher",
    }
