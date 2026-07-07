"""Phase I — error taxonomy contract."""

from __future__ import annotations

import logging

import pytest

from hermes_agent.observability import (
    DEGRADE_TAG,
    DROP_TAG,
    FALLBACK_TAG,
    FatalError,
    ObservabilityEvent,
    RecoverableError,
    log_degrade,
    log_drop,
    log_fallback,
)


def test_recoverable_and_fatal_disjoint():
    assert not issubclass(RecoverableError, FatalError)
    assert not issubclass(FatalError, RecoverableError)


def test_recoverable_is_exception():
    assert issubclass(RecoverableError, Exception)
    assert issubclass(FatalError, Exception)


def test_log_drop_emits_event_tag(caplog):
    logger = logging.getLogger("test-drop")
    with caplog.at_level(logging.WARNING, logger="test-drop"):
        event = log_drop(logger, reason="malformed_frame", code="E4021", frame_type="junk")

    assert isinstance(event, ObservabilityEvent)
    assert event.event == DROP_TAG
    assert event.fields["reason"] == "malformed_frame"
    assert event.fields["code"] == "E4021"
    assert event.fields["frame_type"] == "junk"
    assert any("event=drop" in rec.getMessage() for rec in caplog.records)


def test_log_degrade_emits_event_tag(caplog):
    logger = logging.getLogger("test-degrade")
    with caplog.at_level(logging.WARNING, logger="test-degrade"):
        event = log_degrade(
            logger,
            from_state="WAL",
            to_state="DELETE",
            cause="NFS_mount",
            db="/tmp/state.db",
        )

    assert event.event == DEGRADE_TAG
    assert event.fields["from"] == "WAL"
    assert event.fields["to"] == "DELETE"
    assert event.fields["cause"] == "NFS_mount"
    assert any("event=degrade" in rec.getMessage() for rec in caplog.records)


def test_log_fallback_emits_event_tag(caplog):
    logger = logging.getLogger("test-fallback")
    with caplog.at_level(logging.INFO, logger="test-fallback"):
        event = log_fallback(
            logger,
            path="history-to-engine-events",
            capability_missing="toolEvents.canonical",
        )

    assert event.event == FALLBACK_TAG
    assert event.fields["path"] == "history-to-engine-events"
    assert event.fields["capability_missing"] == "toolEvents.canonical"


def test_recoverable_can_be_caught_specifically():
    """Callers can do ``except RecoverableError`` without swallowing everything."""

    def _fn():
        raise RecoverableError("transient")

    with pytest.raises(RecoverableError):
        _fn()


def test_fatal_can_be_caught_specifically():
    def _fn():
        raise FatalError("invariant violated")

    with pytest.raises(FatalError):
        _fn()
