"""Repository/application contracts for durable delivery obligations."""

from __future__ import annotations

from hermes_agent.composition.cli_session_store import open_cli_session_store
from hermes_agent.domain.delivery_obligation import DeliveryObligationState


def _service(tmp_path):
    store = open_cli_session_store(tmp_path / "state.db")
    return store, store.delivery_obligations


def _record(service, **overrides):
    fields = {
        "session_key": "agent:main:slack:channel:C1",
        "inbound_message_id": "msg-1",
        "platform": "slack",
        "chat_id": "C1",
        "thread_id": "T1",
        "reply_to": "msg-1",
        "metadata": {"thread_id": "T1", "notify": True},
        "content": "final answer",
        "owner_pid": 111,
        "owner_started_at": 222,
        "now": 1000.0,
    }
    fields.update(overrides)
    return service.record(**fields)


def test_record_and_delivery_state_machine(tmp_path):
    store, service = _service(tmp_path)
    try:
        obligation_id = _record(service)
        assert service.mark_attempting(obligation_id)
        assert service.mark_delivered(obligation_id)
        row = service.list_recent()[0]
    finally:
        store.close()

    assert row.state is DeliveryObligationState.DELIVERED
    assert row.metadata == {"thread_id": "T1", "notify": True}
    assert row.reply_to == "msg-1"


def test_record_is_idempotent_and_never_reopens_delivered_row(tmp_path):
    store, service = _service(tmp_path)
    try:
        obligation_id = _record(service)
        service.mark_delivered(obligation_id)
        duplicate_id = _record(service, now=2000.0)
        row = service.list_recent()[0]
    finally:
        store.close()

    assert duplicate_id == obligation_id
    assert row.state is DeliveryObligationState.DELIVERED
    assert row.created_at == 1000.0


def test_distinct_thread_or_message_produces_distinct_obligation(tmp_path):
    store, service = _service(tmp_path)
    try:
        first = _record(service)
        second = _record(
            service,
            session_key="agent:main:slack:channel:C1:T2",
            thread_id="T2",
        )
        third = _record(service, inbound_message_id="msg-2")
    finally:
        store.close()

    assert len({first, second, third}) == 3


def test_dead_owner_pending_row_is_claimed_without_duplicate_marker(tmp_path):
    store, service = _service(tmp_path)
    try:
        _record(service)
        claimed = service.claim_recoverable(
            owner_pid=333,
            owner_started_at=444,
            owner_alive=lambda _pid, _started: False,
            now=1100.0,
        )
    finally:
        store.close()

    assert len(claimed) == 1
    assert claimed[0].needs_duplicate_marker is False
    assert claimed[0].obligation.attempts == 1
    assert claimed[0].obligation.owner_pid == 333


def test_attempting_or_failed_row_is_claimed_with_duplicate_marker(tmp_path):
    store, service = _service(tmp_path)
    try:
        obligation_id = _record(service)
        service.mark_attempting(obligation_id)
        claimed = service.claim_recoverable(
            owner_pid=333,
            owner_started_at=444,
            owner_alive=lambda _pid, _started: False,
            now=1100.0,
        )
    finally:
        store.close()

    assert len(claimed) == 1
    assert claimed[0].needs_duplicate_marker is True


def test_live_owner_is_not_claimed(tmp_path):
    store, service = _service(tmp_path)
    try:
        _record(service)
        claimed = service.claim_recoverable(
            owner_pid=333,
            owner_started_at=444,
            owner_alive=lambda _pid, _started: True,
            now=1100.0,
        )
    finally:
        store.close()

    assert claimed == []


def test_stale_row_is_abandoned(tmp_path):
    store, service = _service(tmp_path)
    try:
        _record(service, now=0.0)
        claimed = service.claim_recoverable(
            owner_pid=333,
            owner_started_at=444,
            owner_alive=lambda _pid, _started: False,
            now=service.STALE_AFTER_SECONDS + 1,
        )
        row = service.list_recent()[0]
    finally:
        store.close()

    assert claimed == []
    assert row.state is DeliveryObligationState.ABANDONED


def test_disconnected_platform_does_not_spend_redelivery_budget(tmp_path):
    store, service = _service(tmp_path)
    try:
        obligation_id = _record(service, platform="telegram")
        service.mark_attempting(obligation_id)
        for index in range(service.MAX_ATTEMPTS + 2):
            claimed = service.claim_recoverable(
                owner_pid=333 + index,
                owner_started_at=444 + index,
                owner_alive=lambda _pid, _started: False,
                deliverable_platforms={"discord"},
                now=1100.0 + index,
            )
            assert claimed == []
        row = service.list_recent()[0]
    finally:
        store.close()

    assert row.attempts == 0
    assert row.state is DeliveryObligationState.ATTEMPTING


def test_obligation_is_claimed_when_platform_returns(tmp_path):
    store, service = _service(tmp_path)
    try:
        _record(service, platform="telegram")
        unavailable = service.claim_recoverable(
            owner_pid=333,
            owner_started_at=444,
            owner_alive=lambda _pid, _started: False,
            deliverable_platforms={"discord"},
            now=1100.0,
        )
        available = service.claim_recoverable(
            owner_pid=555,
            owner_started_at=666,
            owner_alive=lambda _pid, _started: False,
            deliverable_platforms={"telegram"},
            now=1200.0,
        )
    finally:
        store.close()

    assert unavailable == []
    assert len(available) == 1
    assert available[0].obligation.attempts == 1


def test_stale_unavailable_obligation_is_still_abandoned(tmp_path):
    store, service = _service(tmp_path)
    try:
        _record(service, platform="telegram", now=0.0)
        claimed = service.claim_recoverable(
            owner_pid=333,
            owner_started_at=444,
            owner_alive=lambda _pid, _started: False,
            deliverable_platforms={"discord"},
            now=service.STALE_AFTER_SECONDS + 1,
        )
        row = service.list_recent()[0]
    finally:
        store.close()

    assert claimed == []
    assert row.state is DeliveryObligationState.ABANDONED
