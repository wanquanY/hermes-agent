"""stdio_daemon as an actual OS subprocess.

The in-process tests in ``test_stdio_daemon.py`` drive ``run_daemon`` with
``io.StringIO`` pairs — that proves the wiring is correct but does NOT
prove the CLI entry point ``python -m hermes_agent.transport.stdio_daemon``
launches, links, and speaks JSONL over real OS pipes.

This test spawns the daemon as a real child process, writes JSONL to its
stdin, reads JSONL from its stdout, and asserts the handshake plus a
round-trip request/response.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_agent.composition.session_repository_db import (
    ensure_session_repository_schema,
)


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


_SCHEMA_MIN = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT,
    display_title TEXT,
    display_title_source TEXT,
    session_kind TEXT NOT NULL DEFAULT 'hermes_session',
    conversation_kind TEXT NOT NULL DEFAULT 'direct',
    parent_session_id TEXT,
    started_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    ended_at REAL,
    end_reason TEXT
);
CREATE TABLE session_index (
    session_id TEXT PRIMARY KEY,
    owner_agent_profile_id TEXT NOT NULL DEFAULT '',
    owner_profile_version_id TEXT NOT NULL DEFAULT '',
    runtime_scope_key TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    preview TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    session_kind TEXT NOT NULL DEFAULT '',
    conversation_kind TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'idle',
    running INTEGER NOT NULL DEFAULT 0,
    waiting_approval INTEGER NOT NULL DEFAULT 0,
    active_run_id TEXT NOT NULL DEFAULT '',
    active_execution_session_id TEXT NOT NULL DEFAULT '',
    pending_approval_count INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    last_activity REAL
);
CREATE TABLE session_branches (
    child_session_id TEXT PRIMARY KEY,
    parent_session_id TEXT NOT NULL,
    branch_from_seq INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE agent_profiles (
    id TEXT PRIMARY KEY, slug TEXT NOT NULL, name TEXT NOT NULL,
    avatar TEXT, description TEXT, category TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    is_system_default INTEGER NOT NULL DEFAULT 0,
    hermes_profile_name TEXT, hermes_home_path TEXT NOT NULL DEFAULT '',
    default_model TEXT,
    current_version_id TEXT NOT NULL DEFAULT '',
    current_version_number INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE agent_profile_versions (
    profile_id TEXT NOT NULL, version_id TEXT NOT NULL,
    version_number INTEGER NOT NULL, payload_json TEXT,
    created_at REAL NOT NULL, is_current INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (profile_id, version_id)
);
CREATE TABLE agent_profile_growth_summary (
    profile_id TEXT PRIMARY KEY,
    total_runs INTEGER NOT NULL DEFAULT 0,
    total_messages INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    growth_score REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT,
    participant_id TEXT NOT NULL DEFAULT '', tool_call_id TEXT,
    tool_calls TEXT, tool_name TEXT, timestamp REAL NOT NULL,
    reasoning TEXT,
    conversation_message_id TEXT NOT NULL DEFAULT '',
    platform_message_id TEXT, metadata_json TEXT,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE runs (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL,
    parent_seq INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL DEFAULT 0, ended_at REAL,
    end_reason TEXT, end_reason_detail TEXT,
    kind TEXT NOT NULL DEFAULT 'agent'
);
CREATE TABLE run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL, session_id TEXT NOT NULL,
    seq INTEGER NOT NULL, kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE seq_counter (
    session_id TEXT PRIMARY KEY,
    next_seq INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE team_missions (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL,
    plan_json TEXT NOT NULL DEFAULT '{}',
    started_at REAL NOT NULL DEFAULT 0, ended_at REAL,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE team_mission_nodes (
    mission_id TEXT NOT NULL, node_id TEXT NOT NULL, status TEXT NOT NULL,
    depends_on_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL DEFAULT '{}',
    started_at REAL, ended_at REAL,
    PRIMARY KEY (mission_id, node_id)
);
"""


def _prep_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA_MIN)
    ensure_session_repository_schema(conn)
    conn.commit()
    conn.close()


def test_stdio_daemon_subprocess_handshake_and_roundtrip(tmp_path):
    """Spawn the daemon as a real child process and prove it speaks
    JSONL over stdin/stdout.
    """
    db = tmp_path / "state.db"
    _prep_db(db)

    stdin_lines = [
        json.dumps(
            {
                "id": "1",
                "method": "session.create",
                "params": {
                    "sessionId": "s1",
                    "source": "subproc-test",
                    "title": "hello",
                },
            }
        ),
        json.dumps(
            {"id": "2", "method": "session.get", "params": {"sessionId": "s1"}}
        ),
        "",  # EOF marker via empty line; the daemon exits on stdin EOF
    ]
    stdin_bytes = ("\n".join(stdin_lines)).encode("utf-8")

    env = os.environ.copy()
    # Ensure the child imports the same tree we're testing, not a system
    # install.
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_agent.transport.stdio_daemon",
            str(db),
        ],
        input=stdin_bytes,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        timeout=15,
    )
    assert proc.returncode == 0, (
        f"daemon exited non-zero: {proc.returncode}\nSTDERR:\n{proc.stderr.decode('utf-8', 'ignore')}"
    )

    lines = [
        line
        for line in proc.stdout.decode("utf-8").splitlines()
        if line.strip()
    ]
    # 1 handshake + 2 responses = 3 lines.
    assert len(lines) == 3, (
        f"expected 1 handshake + 2 responses, got {len(lines)} lines:\n"
        + "\n".join(lines)
    )
    handshake = json.loads(lines[0])
    resp_create = json.loads(lines[1])
    resp_get = json.loads(lines[2])

    assert handshake["type"] == "handshake"
    assert handshake["contractVersion"] == "3.1"

    assert resp_create["id"] == "1"
    assert resp_create["type"] == "response"
    assert resp_create["result"]["session_id"] == "s1"
    assert resp_create["result"]["source"] == "subproc-test"
    assert resp_create["result"]["title"] == "hello"

    assert resp_get["id"] == "2"
    assert resp_get["type"] == "response"
    assert resp_get["result"]["session_id"] == "s1"
    assert resp_get["result"]["title"] == "hello"


def test_stdio_daemon_subprocess_reports_usage_on_missing_argv(tmp_path):
    """No positional arg → non-zero exit + usage line on stderr."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_agent.transport.stdio_daemon"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        timeout=10,
    )
    assert proc.returncode == 2
    assert b"usage" in proc.stderr.lower(), proc.stderr
