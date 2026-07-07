"""Executable v3 stack — reads JSONL requests on stdin, writes JSONL on stdout.

Usage::

    python -m hermes_agent.transport.stdio_daemon <sqlite_path>

The daemon wires the full 5-layer stack (Transport → Router → dispatch →
orchestrator → repo → SQLite) against the supplied SQLite file and pumps
inbound frames until EOF. Ideal for manual testing::

    $ python -m hermes_agent.transport.stdio_daemon /tmp/state.db <<EOF
    {"id":"1","method":"session.create","params":{"sessionId":"s1","source":"test"}}
    {"id":"2","method":"session.get","params":{"sessionId":"s1"}}
    EOF
    {"type":"handshake",...}
    {"id":"1","type":"response","result":{...}}
    {"id":"2","type":"response","result":{...}}

Every response is line-flushed so the peer can read one JSON per line
without buffering.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from typing import IO

from hermes_agent.gateway import (
    AllowAllResolver,
    MethodRegistry,
    PermissionResolver,
)
from hermes_agent.gateway.methods import (
    agent_profile_methods,
    handshake_method,
    message_methods,
    run_methods,
    session_methods,
    team_mission_methods,
)
from hermes_agent.gateway.methods.run_methods import register_lifecycle
from hermes_agent.gateway.methods.team_mission_methods import register_graph
from hermes_agent.orchestration import (
    RunOrchestrator,
    TeamMissionOrchestrator,
    WorkerPool,
)
from hermes_agent.repositories import (
    AgentProfileRepoImpl,
    MessageRepoImpl,
    RunRepoImpl,
    SessionRepoImpl,
    TeamMissionRepoImpl,
)
from hermes_agent.transport import (
    StdioTransport,
    TransportRouter,
)


_logger = logging.getLogger(__name__)


def build_registry_and_router(
    conn: sqlite3.Connection,
    *,
    in_stream: IO[str],
    out_stream: IO[str],
    resolver: PermissionResolver | None = None,
) -> tuple[MethodRegistry, TransportRouter]:
    """Wire the full v3 stack against ``conn`` and return the registry +
    router pair. Callers own the connection and are responsible for closing
    it.

    ``resolver`` defaults to ``AllowAllResolver`` — stdio daemons run inside
    the process's trust boundary. Production deployments must swap in a real
    resolver (spec §J8).
    """
    session_repo = SessionRepoImpl(conn)
    run_repo = RunRepoImpl(conn)
    message_repo = MessageRepoImpl(conn)
    mission_repo = TeamMissionRepoImpl(conn)
    profile_repo = AgentProfileRepoImpl(conn)
    mission_orch = TeamMissionOrchestrator(mission_repo)
    run_orch = RunOrchestrator(WorkerPool())

    def conn_provider(_session_id):
        return conn

    registry = MethodRegistry()
    handshake_method.register(registry)
    session_methods.register(registry, session_repo)
    run_methods.register(registry, run_repo, conn_provider=conn_provider)
    register_lifecycle(registry, run_orch, conn_provider)
    message_methods.register(registry, message_repo, conn_provider=conn_provider)
    team_mission_methods.register(registry, mission_orch)
    register_graph(registry, mission_repo)
    agent_profile_methods.register(registry, profile_repo, conn_provider=conn_provider)

    transport = StdioTransport(in_stream, out_stream)
    router = TransportRouter(
        transport=transport,
        registry=registry,
        resolver=resolver or AllowAllResolver(),
    )
    return registry, router


def run_daemon(
    conn: sqlite3.Connection,
    *,
    in_stream: IO[str],
    out_stream: IO[str],
    max_frames: int = 10_000_000,
    resolver: PermissionResolver | None = None,
) -> dict:
    """Run the daemon loop until the transport signals EOF.

    Returns a small summary dict of what was processed. ``max_frames`` is a
    defensive cap — real deployments leave it at the default; tests can
    lower it to bound the loop.
    """
    _, router = build_registry_and_router(
        conn,
        in_stream=in_stream,
        out_stream=out_stream,
        resolver=resolver,
    )
    router.start()

    processed = 0
    while processed < max_frames:
        if not router.pump_one():
            break
        processed += 1

    router.stop()
    stats = router.stats()
    return {
        "handshakes_sent": stats.handshakes_sent,
        "requests_dispatched": stats.requests_dispatched,
        "errors_returned": stats.errors_returned,
        "frames_processed": processed,
    }


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 1:
        print(
            "usage: python -m hermes_agent.transport.stdio_daemon <sqlite_path>",
            file=sys.stderr,
        )
        return 2

    db_path = argv[0]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        summary = run_daemon(conn, in_stream=sys.stdin, out_stream=sys.stdout)
    finally:
        conn.close()

    _logger.info(
        "stdio daemon exited: %d frames, %d dispatched, %d errors",
        summary["frames_processed"],
        summary["requests_dispatched"],
        summary["errors_returned"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
