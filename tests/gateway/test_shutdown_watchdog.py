"""Out-of-loop shutdown and event-loop liveness contracts."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from unittest.mock import patch

import pytest

from hermes_gateway.shutdown_watchdog import (
    DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S,
    arm_shutdown_watchdog,
    get_loop_heartbeat_path,
    get_shutdown_watchdog_dump_path,
    loop_heartbeat_forever,
    resolve_shutdown_watchdog_delay,
    write_loop_heartbeat,
)


def test_resolve_shutdown_watchdog_delay_adds_grace():
    assert resolve_shutdown_watchdog_delay(180) == (
        180 + DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    )
    assert resolve_shutdown_watchdog_delay(0) == DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    assert resolve_shutdown_watchdog_delay("bad") == DEFAULT_SHUTDOWN_WATCHDOG_GRACE_S
    assert resolve_shutdown_watchdog_delay(10, grace_s=5) == 15.0


def test_write_loop_heartbeat_atomic_json(tmp_path):
    path = write_loop_heartbeat(pid=4242, start_time=100.5, home=tmp_path)
    assert path == tmp_path / "state" / "gateway.heartbeat"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["pid"] == 4242
    assert data["start_time"] == 100.5
    assert "updated_at" in data
    assert "monotonic" in data
    assert get_loop_heartbeat_path(tmp_path) == path


def test_arm_shutdown_watchdog_disarms_before_fire(tmp_path):
    done = threading.Event()
    exited = []
    with patch("hermes_gateway.shutdown_watchdog.os._exit", side_effect=exited.append):
        arm_shutdown_watchdog(
            0.4,
            done_event=done,
            dump_path=tmp_path / "dump.log",
            exit_code=7,
        )
        time.sleep(0.1)
        done.set()
        time.sleep(0.5)
    assert exited == []


def test_arm_shutdown_watchdog_fires_with_dump_and_exit(tmp_path):
    done = threading.Event()
    fired = threading.Event()
    dump = tmp_path / "logs" / "watchdog.log"
    exit_codes = []

    def _exit(code):
        exit_codes.append(code)
        fired.set()

    with (
        patch("hermes_gateway.shutdown_watchdog.os._exit", side_effect=_exit),
        patch("channels.runtime_status.remove_pid_file"),
        patch("channels.runtime_status.release_gateway_runtime_lock"),
    ):
        arm_shutdown_watchdog(
            0.15,
            done_event=done,
            snapshot_fn=lambda: {"running_agents": 1, "draining": True},
            dump_path=dump,
            exit_code=9,
        )
        assert fired.wait(timeout=5.0)

    assert exit_codes == [9]
    text = dump.read_text(encoding="utf-8")
    assert "shutdown_watchdog_fired" in text
    assert "faulthandler dump" in text
    assert (
        get_shutdown_watchdog_dump_path(tmp_path).name
        == "gateway-shutdown-watchdog.log"
    )


@pytest.mark.asyncio
async def test_loop_heartbeat_rewrites_until_cancelled(tmp_path):
    path = get_loop_heartbeat_path(tmp_path)
    task = asyncio.create_task(
        loop_heartbeat_forever(
            interval_s=0.05,
            start_time=12.0,
            home=tmp_path,
        )
    )
    try:
        for _ in range(50):
            if path.is_file():
                break
            await asyncio.sleep(0.02)
        first = path.read_text(encoding="utf-8")
        assert json.loads(first)["start_time"] == 12.0

        second = first
        for _ in range(100):
            await asyncio.sleep(0.03)
            second = path.read_text(encoding="utf-8")
            if second != first:
                break
        assert second != first
        assert json.loads(second)["start_time"] == 12.0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
