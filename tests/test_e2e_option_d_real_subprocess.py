"""Real subprocess E2E coverage for Option D run_worker IPC.

Complements ``test_e2e_option_d_scenarios.py``: that file keeps the fast
fake-supervisor scenario matrix, while this test launches an actual
``tui_gateway.run_worker`` subprocess and exercises the production
stdin/stdout frame codec, supervisor dispatch loop, DB RPC proxy, and clean
worker teardown.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from hermes_agent.composition.cli_session_store import CliSessionStore, open_cli_session_store
from tui_gateway.run_worker import (
    EventFrame,
    LogFrame,
    RunStartFrame,
    RunTerminalFrame,
)
from hermes_agent.orchestration.worker_lease_manager import WorkerLeaseManager
from hermes_agent.orchestration.worker_supervisor import WorkerSupervisor


_SITE_CUSTOMIZE = r'''
from __future__ import annotations

import asyncio

from tui_gateway import run_worker as _run_worker
from tui_gateway.run_worker import (
    EventFrame,
    LogFrame,
    RunTerminalFrame,
    WorkerRunBackend,
)
from hermes_agent.orchestration.worker_db_proxy import get_default_worker_db_proxy


class _Audit5Backend(WorkerRunBackend):
    async def start(self, frame, emit):
        await emit(LogFrame(level="info", text="audit-5 backend: start"))
        db = get_default_worker_db_proxy()
        if db is None:
            raise RuntimeError("worker DB proxy unavailable")

        activity_id = str(frame.params.get("activity_id") or "audit-5-activity")

        def write_initial_state():
            db.sessions.create(frame.conversation_session_id, source="worker")
            db.messages.append(
                frame.conversation_session_id,
                role="user",
                content=frame.prompt,
                metadata={"source": "audit-5-subprocess"},
            )
            db.activities.create(
                activity_id=activity_id,
                conversation_id=frame.conversation_session_id,
                kind="agent_dispatch",
                target_profile_id=frame.params.get("agent_profile_id") or "audit-profile",
                prompt_summary=frame.prompt,
            )
            db.activities.update_status(activity_id, "running", started_at=101.0)

        await asyncio.to_thread(write_initial_state)

        await emit(
            EventFrame(
                params={
                    "type": "message.delta",
                    "run_id": frame.run_id,
                    "turn_id": frame.turn_id,
                    "payload": {"delta": "worker "},
                }
            )
        )

        assistant_text = "worker pong"

        def write_terminal_state():
            db.messages.append(
                frame.conversation_session_id,
                role="assistant",
                content=assistant_text,
                metadata={"usage": {"total_tokens": 2}},
            )
            db.activities.update_status(
                activity_id,
                "completed",
                result_summary=assistant_text,
                result_json={"run_id": frame.run_id, "text": assistant_text},
                completed_at=202.0,
            )

        await asyncio.to_thread(write_terminal_state)

        await emit(
            EventFrame(
                params={
                    "type": "message.complete",
                    "run_id": frame.run_id,
                    "turn_id": frame.turn_id,
                    "payload": {
                        "text": assistant_text,
                        "usage": {"total_tokens": 2},
                    },
                }
            )
        )
        await emit(
            RunTerminalFrame(
                run_id=frame.run_id,
                status="completed",
                conversation_session_id=frame.conversation_session_id,
                turn_id=frame.turn_id,
            )
        )


_run_worker._build_default_backend = lambda: _Audit5Backend()
'''


class _Collector:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, EventFrame]] = []
        self.terminals: list[tuple[str, str, RunTerminalFrame]] = []
        self.logs: list[tuple[str, str, LogFrame]] = []
        self.terminal_received = asyncio.Event()

    async def on_event(
        self,
        scope_key: str,
        conversation_id: str,
        frame: EventFrame,
    ) -> None:
        self.events.append((scope_key, conversation_id, frame))

    async def on_interactive_request(self, *_args: Any) -> None:
        return None

    async def on_run_terminal(
        self,
        scope_key: str,
        conversation_id: str,
        frame: RunTerminalFrame,
    ) -> None:
        self.terminals.append((scope_key, conversation_id, frame))
        self.terminal_received.set()

    async def on_log(
        self,
        scope_key: str,
        conversation_id: str,
        frame: LogFrame,
    ) -> None:
        self.logs.append((scope_key, conversation_id, frame))


def _install_child_backend(site_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_dir.mkdir()
    (site_dir / "sitecustomize.py").write_text(_SITE_CUSTOMIZE, encoding="utf-8")
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    pythonpath = os.pathsep.join([str(site_dir), str(Path.cwd())])
    if existing_pythonpath:
        pythonpath = os.pathsep.join([pythonpath, existing_pythonpath])
    monkeypatch.setenv("PYTHONPATH", pythonpath)


@pytest.mark.asyncio
async def test_real_run_worker_subprocess_roundtrip_writes_db_via_ipc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_home = tmp_path / "control-home"
    profile_home = tmp_path / "profile-home"
    control_home.mkdir()
    profile_home.mkdir()
    db = open_cli_session_store(control_home / "state.db")

    _install_child_backend(tmp_path / "child-site", monkeypatch)
    monkeypatch.setenv("DOVIE_HERMES_CONTROL_HOME", str(control_home))

    from tui_gateway import server as _server

    monkeypatch.setattr(_server, "_get_db", lambda: db, raising=False)
    monkeypatch.setattr(
        _server,
        "_db_for_stable_session",
        lambda _stable: db,
        raising=False,
    )

    collector = _Collector()
    supervisor = WorkerSupervisor(
        on_event=collector.on_event,
        on_interactive_request=collector.on_interactive_request,
        on_run_terminal=collector.on_run_terminal,
        on_log=collector.on_log,
    )
    pool = WorkerLeaseManager(supervisor, reap_tick_s=60)

    conversation_id = "audit-5-conversation"
    run_id = "audit-5-run"
    turn_id = "audit-5-turn"
    activity_id = "audit-5-activity"
    worker = None
    try:
        lease = await pool.get_or_spawn(
            conversation_id,
            {
                "agent_profile_id": "audit-profile",
                "runtime_scope_key": "profile:audit-profile",
                "hermes_home": str(profile_home),
            },
        )
        worker = lease.worker
        assert worker.running()
        assert worker.process.pid > 0

        await pool.record_run_start(
            conversation_id=conversation_id,
            run_id=run_id,
            conversation_session_id=conversation_id,
            turn_id=turn_id,
        )
        ok = await supervisor.send(
            lease.scope_key,
            lease.worker_conversation_id,
            RunStartFrame(
                run_id=run_id,
                turn_id=turn_id,
                conversation_session_id=conversation_id,
                prompt="ping from audit-5",
                params={
                    "activity_id": activity_id,
                    "agent_profile_id": "audit-profile",
                    "runtime_scope_key": lease.scope_key,
                },
            ),
        )
        assert ok is True
        await pool.release(conversation_id)

        await asyncio.wait_for(collector.terminal_received.wait(), timeout=30.0)
    finally:
        await pool.shutdown()

    assert worker is not None
    assert worker.process.returncode == 0

    assert collector.logs
    assert any(frame.text == "run_worker: started" for _, _, frame in collector.logs)
    assert any(frame.text == "audit-5 backend: start" for _, _, frame in collector.logs)

    payloads = [frame.params for _, _, frame in collector.events]
    assert any(payload.get("type") == "message.delta" for payload in payloads)
    complete_payload = next(
        payload for payload in payloads if payload.get("type") == "message.complete"
    )
    assert complete_payload["run_id"] == run_id
    assert complete_payload["turn_id"] == turn_id
    assert complete_payload["payload"]["text"] == "worker pong"
    assert complete_payload["payload"]["usage"] == {"total_tokens": 2}

    assert len(collector.terminals) == 1
    terminal_scope, terminal_conversation, terminal = collector.terminals[0]
    assert terminal_scope == "profile:audit-profile"
    assert terminal_conversation == conversation_id
    assert terminal.run_id == run_id
    assert terminal.turn_id == turn_id
    assert terminal.conversation_session_id == conversation_id
    assert terminal.status == "completed"

    messages = db.messages.all_as_conversation(conversation_id)
    assert [(message["role"], message["content"]) for message in messages] == [
        ("user", "ping from audit-5"),
        ("assistant", "worker pong"),
    ]

    activity = db.activities.get(activity_id)
    assert activity is not None
    assert activity["conversation_id"] == conversation_id
    assert activity["kind"] == "agent_dispatch"
    assert activity["target_profile_id"] == "audit-profile"
    assert activity["status"] == "completed"
    assert activity["result_summary"] == "worker pong"
