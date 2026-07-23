"""Tests for the run-worker structured logging transport."""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

import hermes_logging
from tui_gateway.worker_logging import WorkerLogBridge, WorkerLogEnvelope


@pytest.mark.asyncio
async def test_bridge_forwards_threaded_record_with_context_and_exception() -> None:
    emitted: list[WorkerLogEnvelope] = []

    async def emit(envelope: WorkerLogEnvelope) -> None:
        emitted.append(envelope)

    bridge = WorkerLogBridge(emit)
    bridge.start()
    logger = logging.getLogger("tests.worker.forwarded")
    old_level = logger.level
    logger.setLevel(logging.INFO)

    def write_log() -> None:
        hermes_logging.set_session_context("session-worker-1")
        try:
            raise ValueError("worker exploded")
        except ValueError:
            logger.exception("forward me")
        finally:
            hermes_logging.clear_session_context()

    thread = threading.Thread(target=write_log, name="agent-thread-test")
    try:
        thread.start()
        thread.join()
        await asyncio.sleep(0)
        await bridge.close()
    finally:
        logger.setLevel(old_level)
        if thread.is_alive():
            thread.join(timeout=1)

    matching = [item for item in emitted if item.logger == logger.name]
    assert len(matching) == 1
    record = matching[0]
    assert record.level == "error"
    assert record.text == "forward me"
    assert record.thread_name == "agent-thread-test"
    assert record.session_tag == " [session-worker-1]"
    assert "ValueError: worker exploded" in record.exception
    assert record.pathname.endswith("test_worker_logging.py")
    assert record.line_no > 0


@pytest.mark.asyncio
async def test_bridge_reports_low_priority_queue_overflow() -> None:
    release = asyncio.Event()
    emitted: list[WorkerLogEnvelope] = []

    async def emit(envelope: WorkerLogEnvelope) -> None:
        emitted.append(envelope)
        if envelope.text == "first":
            await release.wait()

    bridge = WorkerLogBridge(emit, queue_size=1)
    bridge.start()
    logger = logging.getLogger("tests.worker.overflow")
    old_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        logger.info("first")
        await asyncio.sleep(0.05)
        logger.info("queued")
        logger.info("dropped")
        release.set()
        await bridge.close()
    finally:
        logger.setLevel(old_level)

    assert any(item.text == "first" for item in emitted)
    assert any(item.text == "queued" for item in emitted)
    summaries = [
        item for item in emitted
        if "worker log transport dropped" in item.text
    ]
    assert summaries
    assert summaries[-1].level == "warning"


@pytest.mark.asyncio
async def test_bridge_removes_handler_on_close() -> None:
    async def emit(_envelope: WorkerLogEnvelope) -> None:
        return None

    bridge = WorkerLogBridge(emit)
    bridge.start()
    assert bridge.handler in logging.getLogger().handlers
    await bridge.close()
    assert bridge.handler not in logging.getLogger().handlers
