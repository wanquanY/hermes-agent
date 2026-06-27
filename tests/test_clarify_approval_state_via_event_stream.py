from __future__ import annotations

from typing import Any

import pytest

from tui_gateway.run_worker import EventFrame
from tui_gateway.services.worker_frame_router import WorkerFrameRouter


class _FakeSender:
    async def send(self, scope_key: str, conversation_id: str, frame: Any) -> bool:
        return True


def _make_router():
    events: list[dict[str, Any]] = []

    def publish_event(payload: dict[str, Any], **kwargs: Any) -> list[Any]:
        events.append({"payload": payload, "kwargs": kwargs})
        return []

    router = WorkerFrameRouter(
        sender=_FakeSender(),
        publish_event=publish_event,
        publish_run_terminal=lambda **kwargs: {},
    )
    return router, events


def _capture_projection(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def project(session_key: str, *, present: bool, source_event_type: str) -> None:
        calls.append({
            "session_key": session_key,
            "present": present,
            "source_event_type": source_event_type,
        })

    monkeypatch.setattr(
        "hermes_team_mission.runtime.approval_observer.project_clarify_or_approval_state",
        project,
    )
    return calls


@pytest.mark.asyncio
async def test_clarify_request_event_triggers_project_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_projection(monkeypatch)
    router, events = _make_router()

    await router.on_event(
        "scope-1",
        "conv-1",
        EventFrame(params={"type": "clarify.request", "stored_session_id": "sess-1"}),
    )

    assert events[0]["payload"]["type"] == "clarify.request"
    assert calls == [
        {
            "session_key": "sess-1",
            "present": True,
            "source_event_type": "clarify.request",
        }
    ]


@pytest.mark.asyncio
async def test_clarify_resolved_event_triggers_project_state_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_projection(monkeypatch)
    router, _events = _make_router()

    await router.on_event(
        "scope-1",
        "conv-1",
        EventFrame(params={"type": "clarify.resolved", "stored_session_id": "sess-1"}),
    )

    assert calls == [
        {
            "session_key": "sess-1",
            "present": False,
            "source_event_type": "clarify.resolved",
        }
    ]


@pytest.mark.asyncio
async def test_approval_request_event_triggers_project_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_projection(monkeypatch)
    router, _events = _make_router()

    await router.on_event(
        "scope-1",
        "conv-1",
        EventFrame(params={"type": "approval.request", "stored_session_id": "sess-2"}),
    )

    assert calls == [
        {
            "session_key": "sess-2",
            "present": True,
            "source_event_type": "approval.request",
        }
    ]


@pytest.mark.asyncio
async def test_unrelated_event_type_does_not_trigger_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_projection(monkeypatch)
    router, events = _make_router()

    await router.on_event(
        "scope-1",
        "conv-1",
        EventFrame(params={"type": "message.delta", "stored_session_id": "sess-1"}),
    )

    assert events[0]["payload"]["type"] == "message.delta"
    assert calls == []


@pytest.mark.asyncio
async def test_project_state_failure_does_not_break_publish_event(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def project(session_key: str, *, present: bool, source_event_type: str) -> None:
        raise RuntimeError("projection failed")

    monkeypatch.setattr(
        "hermes_team_mission.runtime.approval_observer.project_clarify_or_approval_state",
        project,
    )
    router, events = _make_router()

    await router.on_event(
        "scope-1",
        "conv-1",
        EventFrame(params={"type": "approval.resolved", "stored_session_id": "sess-3"}),
    )

    assert events[0]["payload"]["type"] == "approval.resolved"
    assert (
        "clarify/approval projection failed event_type=approval.resolved" in caplog.text
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "expected_session_key"),
    [
        (
            {"type": "approval.request", "stored_session_id": "stored-sess"},
            "stored-sess",
        ),
        ({"type": "approval.request", "session_id": "fallback-sess"}, "fallback-sess"),
        ({"type": "approval.request", "session_key": "legacy-sess"}, "legacy-sess"),
    ],
)
async def test_session_key_resolves_from_stored_session_id_or_session_id_fallback(
    monkeypatch: pytest.MonkeyPatch,
    params: dict[str, Any],
    expected_session_key: str,
) -> None:
    calls = _capture_projection(monkeypatch)
    router, _events = _make_router()

    await router.on_event("scope-1", "conv-1", EventFrame(params=params))

    assert calls[0]["session_key"] == expected_session_key
