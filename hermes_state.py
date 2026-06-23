#!/usr/bin/env python3
"""
SQLite State Store for Hermes Agent.

Provides persistent session storage with FTS5 full-text search, replacing
the per-session JSONL file approach. Stores session metadata, full message
history, and model configuration for CLI and gateway sessions.

Key design decisions:
- WAL mode for concurrent readers + one writer (gateway multi-platform)
- FTS5 virtual table for fast text search across all session messages
- Compression-triggered session splitting via parent_session_id chains
- Batch runner and RL trajectories are NOT stored here (separate systems)
- Session source tagging ('cli', 'telegram', 'discord', etc.) for filtering
"""

import json
import logging
import random
import re
import sqlite3
import threading
import time
from pathlib import Path

from agent.memory_manager import sanitize_context
from hermes_constants import get_hermes_home
from hermes_state_agent_profiles import SessionDBAgentProfileMixin
from hermes_state_branch import SessionDBBranchMixin
from hermes_state_runs import SessionDBRunMixin
from hermes_state_team_capabilities import SessionDBTeamCapabilityMixin
from hermes_state_team_missions import SessionDBTeamMissionMixin
from hermes_state_team_registry import SessionDBTeamRegistryMixin
from hermes_team_mission_conversation_state import prune_empty_team_mission_conversations
from hermes_team_mission_conversation_state import repair_placeholder_team_mission_conversation_titles
from hermes_team_mission_conversation_state import repair_legacy_team_mission_conversation_sessions
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_DB_PATH = get_hermes_home() / "state.db"

SCHEMA_VERSION = 23

# ---------------------------------------------------------------------------
# WAL-compatibility fallback
# ---------------------------------------------------------------------------
# SQLite's WAL mode requires shared-memory (mmap) coordination and fcntl
# byte-range locks that don't reliably work on network filesystems (NFS,
# SMB/CIFS, some FUSE mounts, WSL1).  Upstream documents this explicitly:
# https://www.sqlite.org/wal.html#sometimes_queries_return_sqlite_busy_in_wal_mode
#
# On those filesystems ``PRAGMA journal_mode=WAL`` raises
# ``sqlite3.OperationalError: locking protocol`` (SQLITE_PROTOCOL).  If we
# propagate that, every feature backed by state.db / kanban.db breaks
# silently — /resume, /title, /history, /branch, kanban dispatcher, etc.
#
# Instead, fall back to ``journal_mode=DELETE`` (the pre-WAL default) which
# works on NFS.  Concurrency drops — concurrent readers are blocked during
# a write — but the feature works.
_WAL_INCOMPAT_MARKERS = (
    "locking protocol",       # SQLITE_PROTOCOL on NFS/SMB
    "not authorized",         # Some FUSE mounts block WAL pragma outright
    "disk i/o error",         # Flaky network FS during WAL setup
)

# Last SessionDB() init error, per-process.  Surfaced in /resume and
# related slash-command error strings so users know WHY the DB is
# unavailable instead of getting a bare "Session database not available."
# Only SessionDB.__init__ writes to this; kanban_db.connect() failures
# do not update it (by design — kanban failures are reported via their
# own caller's error handling, not via /resume-style slash commands).
_last_init_error: Optional[str] = None
_last_init_error_lock = threading.Lock()

# Paths for which we've already logged a WAL-fallback WARNING.  Without
# this, kanban_db.connect() (called on every kanban operation — see
# hermes_cli/kanban_db.py for ~30 call sites) would re-log the same
# filesystem-incompat warning on every connection, filling errors.log.
_wal_fallback_warned_paths: set[str] = set()
_wal_fallback_warned_lock = threading.Lock()


def _set_last_init_error(msg: Optional[str]) -> None:
    """Record (or clear) the most recent state.db init failure.

    Thread-safe via _last_init_error_lock.  Callers pass a message to
    record a failure or None to clear.  SessionDB.__init__ only calls
    this to SET on failure — it deliberately does NOT clear on success,
    because in a multi-threaded caller (e.g. gateway / web_server per-
    request SessionDB() instantiation), a concurrent successful open
    racing past a different thread's failure would erase the cause
    string that thread's /resume handler is about to format.  Explicit
    clears (e.g. test fixtures) are still supported by passing None.
    """
    global _last_init_error
    with _last_init_error_lock:
        _last_init_error = msg


def get_last_init_error() -> Optional[str]:
    """Return the most recent state.db init failure, if any.

    Slash-command handlers (``/resume``, ``/title``, ``/history``, ``/branch``)
    call this to surface the underlying cause in their error messages when
    ``_session_db is None``.  Returns ``None`` if SessionDB initialized
    successfully (or hasn't been attempted).
    """
    return _last_init_error


def format_session_db_unavailable(prefix: str = "Session database not available") -> str:
    """Format a user-facing 'session DB unavailable' message with cause.

    When ``SessionDB()`` init fails, callers set ``_session_db = None`` and
    several slash commands (/resume, /title, /history, /branch) previously
    responded with a bare ``"Session database not available."`` — no
    indication of WHY.  This helper includes the captured cause (typically
    ``"locking protocol"`` from NFS/SMB) and points users at the known
    culprit so they can fix it themselves.

    Example output:
        Session database not available: locking protocol (state.db may be
        on NFS/SMB — see https://www.sqlite.org/wal.html).
    """
    cause = get_last_init_error()
    if not cause:
        return f"{prefix}."
    hint = ""
    if any(marker in cause.lower() for marker in _WAL_INCOMPAT_MARKERS):
        hint = " (state.db may be on NFS/SMB/FUSE — see https://www.sqlite.org/wal.html)"
    return f"{prefix}: {cause}{hint}."


def apply_wal_with_fallback(
    conn: sqlite3.Connection,
    *,
    db_label: str = "state.db",
) -> str:
    """Set ``journal_mode=WAL`` on ``conn``, falling back to DELETE on failure.

    Returns the journal mode actually set (``"wal"`` or ``"delete"``).

    On WAL-incompatible filesystems (NFS, SMB, some FUSE), SQLite raises
    ``OperationalError("locking protocol")`` when setting WAL.  We fall
    back to DELETE mode — the pre-WAL default, which works on NFS — and
    log one WARNING explaining why.

    The WARNING is deduplicated per ``db_label``: repeated connections
    to the same underlying DB (e.g. kanban_db.connect() which is called
    on every kanban operation) log once per process, not once per call.
    Different db_labels log independently, so state.db and kanban.db
    each get one warning on the same NFS mount.

    Shared by :class:`SessionDB` and ``hermes_cli.kanban_db.connect`` so
    both databases get identical fallback behavior.
    """
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        return "wal"
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if not any(marker in msg for marker in _WAL_INCOMPAT_MARKERS):
            # Unrelated OperationalError — don't silently swallow.
            raise
        _log_wal_fallback_once(db_label, exc)
        conn.execute("PRAGMA journal_mode=DELETE")
        return "delete"


def _log_wal_fallback_once(db_label: str, exc: Exception) -> None:
    """Log a single WARNING per (process, db_label) about WAL fallback.

    Without this dedup, NFS users running kanban (which opens a fresh
    connection on every operation — see hermes_cli/kanban_db.py) would
    fill errors.log with hundreds of identical warnings per hour.
    """
    with _wal_fallback_warned_lock:
        if db_label in _wal_fallback_warned_paths:
            return
        _wal_fallback_warned_paths.add(db_label)
    logger.warning(
        "%s: WAL journal_mode unsupported on this filesystem (%s) — "
        "falling back to journal_mode=DELETE (slower rollback-journal "
        "mode; reduces concurrency but works on NFS/SMB/FUSE). See "
        "https://www.sqlite.org/wal.html for details. This warning "
        "fires once per process per database.",
        db_label,
        exc,
    )

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    preview TEXT DEFAULT '',
    last_active REAL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    display_title TEXT DEFAULT '',
    display_title_source TEXT DEFAULT '',
    api_call_count INTEGER DEFAULT 0,
    handoff_state TEXT,
    handoff_platform TEXT,
    handoff_error TEXT,
    rewind_count INTEGER NOT NULL DEFAULT 0,
    transient INTEGER DEFAULT 0,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

-- Control-plane denormalized session index. One row per user-visible session.
-- Status fields are a WRITE-TIME projection so the sidebar read path is a single
-- indexed query (no recursive CTE / live merge / per-session approval / per-profile
-- fan-out). It is a projection of the source of truth (sessions + runs + team
-- mission events) and can always be rebuilt via reconcile_session_index().
CREATE TABLE IF NOT EXISTS session_index (
    session_id TEXT PRIMARY KEY,
    owner_agent_profile_id TEXT NOT NULL DEFAULT '',
    owner_profile_version_id TEXT NOT NULL DEFAULT '',
    runtime_scope_key TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    preview TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'unknown',
    transient INTEGER NOT NULL DEFAULT 0,
    session_kind TEXT NOT NULL DEFAULT 'hermes_session',
    status TEXT NOT NULL DEFAULT 'idle',
    running INTEGER NOT NULL DEFAULT 0,
    waiting_approval INTEGER NOT NULL DEFAULT 0,
    active_run_id TEXT NOT NULL DEFAULT '',
    active_runtime_session_id TEXT NOT NULL DEFAULT '',
    pending_approval_count INTEGER NOT NULL DEFAULT 0,
    team_id TEXT NOT NULL DEFAULT '',
    mission_id TEXT NOT NULL DEFAULT '',
    conversation_id TEXT NOT NULL DEFAULT '',
    message_count INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    last_activity REAL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT,
    codex_message_items TEXT,
    platform_message_id TEXT,
    metadata_json TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS session_lineage (
    session_id TEXT PRIMARY KEY,
    parent_session_id TEXT,
    root_session_id TEXT NOT NULL,
    branch_from_message_row_id INTEGER,
    branch_from_turn_id TEXT,
    branch_from_run_id TEXT,
    branch_from_client_message_id TEXT,
    branch_origin TEXT NOT NULL,
    branch_mode TEXT NOT NULL,
    branch_depth INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id),
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS session_branch_requests (
    idempotency_key TEXT PRIMARY KEY,
    source_session_id TEXT NOT NULL,
    branch_fingerprint TEXT NOT NULL,
    result_session_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (source_session_id) REFERENCES sessions(id),
    FOREIGN KEY (result_session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    runtime_scope_key TEXT,
    turn_id TEXT,
    runtime_session_id TEXT,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL,
    last_seq INTEGER DEFAULT 0,
    error TEXT,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    run_id TEXT,
    turn_id TEXT,
    runtime_session_id TEXT,
    runtime_scope_key TEXT,
    event_type TEXT NOT NULL,
    seq INTEGER NOT NULL,
    timestamp REAL NOT NULL,
    payload_json TEXT,
    event_json TEXT NOT NULL,
    status TEXT,
    UNIQUE(session_id, seq)
);

CREATE TABLE IF NOT EXISTS run_event_archives (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    run_id TEXT,
    archived_at REAL NOT NULL,
    first_seq INTEGER NOT NULL,
    last_seq INTEGER NOT NULL,
    first_timestamp REAL NOT NULL,
    last_timestamp REAL NOT NULL,
    event_count INTEGER NOT NULL,
    reason TEXT NOT NULL,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS agent_teams (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    avatar_json TEXT,
    description TEXT,
    lead_agent_profile_id TEXT,
    default_mode TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_team_members (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL REFERENCES agent_teams(id) ON DELETE CASCADE,
    agent_profile_id TEXT NOT NULL,
    agent_profile_version_id TEXT,
    profile_name TEXT,
    profile_avatar TEXT,
    role TEXT NOT NULL,
    capability_tags_json TEXT NOT NULL,
    auto_assignable INTEGER NOT NULL,
    max_concurrent_nodes INTEGER NOT NULL,
    permission_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(team_id, agent_profile_id)
);

CREATE TABLE IF NOT EXISTS agent_profiles (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    avatar TEXT,
    description TEXT,
    category TEXT,
    tags_json TEXT NOT NULL,
    status TEXT NOT NULL,
    is_system_default INTEGER NOT NULL,
    hermes_profile_name TEXT,
    hermes_home_path TEXT NOT NULL,
    default_model TEXT,
    default_provider TEXT,
    default_permission_mode TEXT,
    default_toolsets_json TEXT NOT NULL,
    recommended_skills_json TEXT NOT NULL DEFAULT '[]',
    platform_base_toolsets_initialized INTEGER NOT NULL,
    current_version_id TEXT,
    current_version_number INTEGER NOT NULL,
    source_kind TEXT,
    public_profile_id TEXT,
    public_version_id TEXT,
    public_content_hash TEXT,
    metadata_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_used_at REAL
);

CREATE TABLE IF NOT EXISTS agent_profile_drafts (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    draft_kind TEXT NOT NULL,
    base_agent_profile_id TEXT,
    base_version_id TEXT,
    target_agent_profile_id TEXT,
    source_session_id TEXT,
    source_agent_profile_id TEXT,
    source_run_id TEXT,
    source_turn_id TEXT,
    source_client_message_id TEXT,
    workspace_id TEXT,
    name TEXT NOT NULL,
    avatar TEXT,
    description TEXT,
    category TEXT,
    tags_json TEXT NOT NULL,
    architecture_template_id TEXT,
    recommended_toolsets_json TEXT NOT NULL,
    recommended_skills_json TEXT NOT NULL,
    skill_creation_plans_json TEXT NOT NULL,
    missing_capabilities_json TEXT NOT NULL,
    default_model TEXT,
    default_provider TEXT,
    default_permission_mode TEXT,
    files_json TEXT NOT NULL,
    runtime_prepared_at REAL,
    published_agent_profile_id TEXT,
    published_version_id TEXT,
    metadata_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    published_at REAL
);

CREATE TABLE IF NOT EXISTS team_mission_conversations (
    conversation_id TEXT PRIMARY KEY,
    team_id TEXT,
    stable_session_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    objective TEXT,
    workspace_id TEXT,
    workspace_path TEXT,
    status TEXT NOT NULL,
    active_mission_id TEXT,
    created_by_user_id TEXT,
    metadata_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS team_missions (
    mission_id TEXT PRIMARY KEY,
    conversation_id TEXT,
    team_id TEXT,
    title TEXT NOT NULL,
    objective TEXT,
    workspace_id TEXT,
    workspace_path TEXT,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    leader_session_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS team_mission_nodes (
    node_id TEXT NOT NULL,
    mission_id TEXT NOT NULL REFERENCES team_missions(mission_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    objective TEXT,
    status TEXT NOT NULL,
    assignee_profile_id TEXT,
    assignee_profile_version_id TEXT,
    canonical_node_id TEXT,
    task_frame_id TEXT,
    runtime_stable_session_id TEXT,
    runtime_session_id TEXT,
    runtime_scope_key TEXT,
    output_contract_json TEXT,
    metadata_json TEXT,
    position_x REAL NOT NULL DEFAULT 0,
    position_y REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (mission_id, node_id)
);

CREATE TABLE IF NOT EXISTS team_mission_edges (
    edge_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES team_missions(mission_id) ON DELETE CASCADE,
    from_node_id TEXT NOT NULL,
    to_node_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    metadata_json TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS team_mission_run_bindings (
    run_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES team_missions(mission_id) ON DELETE CASCADE,
    node_id TEXT,
    session_id TEXT NOT NULL,
    runtime_session_id TEXT,
    runtime_scope_key TEXT,
    role TEXT NOT NULL,
    metadata_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS team_mission_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT NOT NULL REFERENCES team_missions(mission_id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    source_event_type TEXT,
    source_run_id TEXT,
    source_session_id TEXT,
    source_seq INTEGER DEFAULT 0,
    dedupe_key TEXT NOT NULL,
    timestamp REAL NOT NULL,
    payload_json TEXT,
    source_event_json TEXT,
    event_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(mission_id, seq),
    UNIQUE(mission_id, dedupe_key)
);

CREATE TABLE IF NOT EXISTS team_mission_artifacts (
    artifact_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES team_missions(mission_id) ON DELETE CASCADE,
    node_id TEXT,
    run_id TEXT,
    tool_call_id TEXT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    uri TEXT NOT NULL,
    mime_type TEXT,
    metadata_json TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS team_mission_memory_items (
    id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    mission_id TEXT NOT NULL REFERENCES team_missions(mission_id) ON DELETE CASCADE,
    conversation_session_id TEXT NOT NULL,
    task_id TEXT,
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    structured_payload_json TEXT,
    source_node_ids_json TEXT,
    source_run_ids_json TEXT,
    artifact_refs_json TEXT,
    workspace_refs_json TEXT,
    confidence REAL NOT NULL,
    visibility TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    invalidated_at REAL
);

CREATE TABLE IF NOT EXISTS team_mission_memory_edges (
    id TEXT PRIMARY KEY,
    from_memory_id TEXT NOT NULL,
    to_memory_id TEXT,
    relation TEXT NOT NULL,
    metadata_json TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS team_capability_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    source_packet_digest TEXT NOT NULL,
    team_profile_json TEXT NOT NULL,
    member_profiles_json TEXT NOT NULL,
    capability_axes_json TEXT NOT NULL,
    assignment_policy_json TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    stale_reason TEXT,
    generated_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(team_id, version)
);

CREATE TABLE IF NOT EXISTS team_capability_snapshot_bindings (
    binding_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES team_missions(mission_id) ON DELETE CASCADE,
    conversation_id TEXT,
    snapshot_id TEXT NOT NULL REFERENCES team_capability_snapshots(snapshot_id) ON DELETE CASCADE,
    snapshot_version INTEGER NOT NULL,
    source_digest TEXT NOT NULL,
    pinned_at REAL NOT NULL,
    UNIQUE(mission_id)
);

"""

# Indexes must be created after _reconcile_columns() runs. SQLite parses index
# definitions immediately; if an existing table is missing an indexed column,
# CREATE INDEX fails before the reconciler can add that column.
DEFERRED_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_sessions_source
    ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_parent
    ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started
    ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_effective_last_active
    ON sessions(COALESCE(last_active, started_at) DESC, started_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_session_index_order
    ON session_index(updated_at DESC, started_at DESC, session_id DESC);
CREATE INDEX IF NOT EXISTS idx_session_index_profile
    ON session_index(owner_agent_profile_id, owner_profile_version_id);
CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_session_active
    ON messages(session_id, active, timestamp);
CREATE INDEX IF NOT EXISTS idx_session_lineage_parent
    ON session_lineage(parent_session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_session_lineage_root
    ON session_lineage(root_session_id, branch_depth, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_session_lineage_branch_point
    ON session_lineage(branch_from_message_row_id);
CREATE INDEX IF NOT EXISTS idx_runs_session_status
    ON runs(session_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_scope_status
    ON runs(runtime_scope_key, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_run_events_session_seq
    ON run_events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_run_events_scope_seq
    ON run_events(runtime_scope_key, session_id, seq);
CREATE INDEX IF NOT EXISTS idx_run_events_run
    ON run_events(run_id, id);
CREATE INDEX IF NOT EXISTS idx_run_event_archives_session
    ON run_event_archives(session_id, archived_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_teams_status_updated
    ON agent_teams(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_team_members_team_id
    ON agent_team_members(team_id, role, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_agent_profiles_status_updated
    ON agent_profiles(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_profile_drafts_status_updated
    ON agent_profile_drafts(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_profile_drafts_source
    ON agent_profile_drafts(source_session_id, source_agent_profile_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_team_mission_conversations_team
    ON team_mission_conversations(team_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_team_mission_conversations_workspace
    ON team_mission_conversations(workspace_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_team_missions_conversation
    ON team_missions(conversation_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_team_missions_status_updated
    ON team_missions(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_team_mission_nodes_mission
    ON team_mission_nodes(mission_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_team_mission_edges_mission
    ON team_mission_edges(mission_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_team_mission_run_bindings_mission
    ON team_mission_run_bindings(mission_id, node_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_team_mission_run_bindings_session
    ON team_mission_run_bindings(session_id, run_id);
CREATE INDEX IF NOT EXISTS idx_team_mission_events_mission_seq
    ON team_mission_events(mission_id, seq ASC);
CREATE INDEX IF NOT EXISTS idx_team_mission_events_source_run
    ON team_mission_events(source_run_id, source_seq);
CREATE INDEX IF NOT EXISTS idx_team_mission_artifacts_mission
    ON team_mission_artifacts(mission_id, node_id, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_team_mission_memory_items_mission
    ON team_mission_memory_items(mission_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_team_mission_memory_items_conversation
    ON team_mission_memory_items(conversation_session_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_team_mission_memory_items_team
    ON team_mission_memory_items(team_id, scope, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_team_mission_memory_edges_from
    ON team_mission_memory_edges(from_memory_id, relation, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_team_mission_memory_edges_to
    ON team_mission_memory_edges(to_memory_id, relation, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_team_capability_snapshots_team
    ON team_capability_snapshots(team_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_team_capability_snapshot_bindings_mission
    ON team_capability_snapshot_bindings(mission_id);
CREATE INDEX IF NOT EXISTS idx_team_capability_snapshot_bindings_conversation
    ON team_capability_snapshot_bindings(conversation_id);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
    INSERT INTO messages_fts(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;
"""

# Trigram FTS5 table for CJK substring search.  The default unicode61
# tokenizer splits CJK characters into individual tokens, breaking phrase
# matching.  The trigram tokenizer creates overlapping 3-byte sequences so
# substring queries work natively for any script (CJK, Thai, etc.).
FTS_TRIGRAM_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
    content,
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts_trigram WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_update AFTER UPDATE ON messages BEGIN
    DELETE FROM messages_fts_trigram WHERE rowid = old.id;
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;
"""


class SessionDB(SessionDBAgentProfileMixin, SessionDBTeamRegistryMixin, SessionDBTeamCapabilityMixin, SessionDBTeamMissionMixin, SessionDBRunMixin, SessionDBBranchMixin):
    """
    SQLite-backed session storage with FTS5 search.

    Thread-safe for the common gateway pattern (multiple reader threads,
    single writer via WAL mode). Each method opens its own cursor.
    """

    # ── Write-contention tuning ──
    # With multiple hermes processes (gateway + CLI sessions + worktree agents)
    # all sharing one state.db, WAL write-lock contention causes visible TUI
    # freezes.  SQLite's built-in busy handler uses a deterministic sleep
    # schedule that causes convoy effects under high concurrency.
    #
    # Instead, we keep the SQLite timeout short (1s) and handle retries at the
    # application level with random jitter, which naturally staggers competing
    # writers and avoids the convoy.
    _WRITE_MAX_RETRIES = 15
    _WRITE_RETRY_MIN_S = 0.020   # 20ms
    _WRITE_RETRY_MAX_S = 0.150   # 150ms
    # Attempt a TRUNCATE WAL checkpoint every N successful writes.
    _CHECKPOINT_EVERY_N_WRITES = 50

    def __init__(self, db_path: Path = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._write_count = 0
        try:
            self._conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                # Short timeout — application-level retry with random jitter
                # handles contention instead of sitting in SQLite's internal
                # busy handler for up to 30s.
                timeout=1.0,
                # Autocommit mode: Python's default isolation_level=""
                # auto-starts transactions on DML, which conflicts with our
                # explicit BEGIN IMMEDIATE.  None = we manage transactions
                # ourselves.
                isolation_level=None,
            )
            self._conn.row_factory = sqlite3.Row
            apply_wal_with_fallback(self._conn, db_label="state.db")
            self._conn.execute("PRAGMA foreign_keys=ON")
            # 增量自动回收:删除产生的空闲页进入 freelist 并被后续写入复用,文件不再
            # 无限膨胀(团队任务的流式 delta「删了不回收」曾把 state.db 撑到 2.5GB、
            # 61% 空洞,连 15 行的会话列表查询都被拖到 ~1.8s)。对新库立即生效;已有的
            # NONE 模式库需 VACUUM 一次切换(运维侧已处理)。必须在建表前设置。
            try:
                self._conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            except Exception:
                pass

            self._init_schema()
            try:
                repaired = repair_legacy_team_mission_conversation_sessions(self)
                if repaired:
                    logger.info(
                        "repaired %d legacy Team Mission conversation session(s)",
                        repaired,
                    )
            except Exception as repair_exc:
                logger.warning(
                    "legacy Team Mission conversation session repair skipped: %s",
                    repair_exc,
                )
            try:
                retitled = repair_placeholder_team_mission_conversation_titles(self)
                if retitled:
                    logger.info(
                        "retitled %d placeholder Team Mission conversation(s)",
                        retitled,
                    )
            except Exception as retitle_exc:
                logger.warning(
                    "placeholder Team Mission conversation title repair skipped: %s",
                    retitle_exc,
                )
            try:
                pruned = prune_empty_team_mission_conversations(self)
                if pruned:
                    logger.info(
                        "pruned %d empty Team Mission conversation shell(s)",
                        pruned,
                    )
            except Exception as prune_exc:
                logger.warning(
                    "empty Team Mission conversation shell prune skipped: %s",
                    prune_exc,
                )
            try:
                repaired_fk_rows = self.repair_orphaned_foreign_key_rows()
                if repaired_fk_rows:
                    logger.info(
                        "repaired %d orphaned state foreign-key row(s)",
                        repaired_fk_rows,
                    )
            except Exception as repair_fk_exc:
                logger.warning(
                    "orphaned state foreign-key repair skipped: %s",
                    repair_fk_exc,
                )
            try:
                self._reclaim_freelist_on_startup()
            except Exception as reclaim_exc:
                logger.warning("state.db freelist reclaim skipped: %s", reclaim_exc)
        except Exception as exc:
            # Capture the cause so /resume and friends can surface WHY the
            # session DB is unavailable instead of a bare "Session database
            # not available."  Callers that catch this exception keep their
            # existing ``self._session_db = None`` degradation path.
            #
            # Note: we deliberately do NOT clear _last_init_error on the
            # success path (no else branch).  In multi-threaded callers
            # (gateway, web_server per-request SessionDB()), a concurrent
            # successful open racing past this failure would erase the
            # cause that another thread's /resume is about to format.
            # Tests that need to reset the state can call
            # ``hermes_state._set_last_init_error(None)`` explicitly.
            _set_last_init_error(f"{type(exc).__name__}: {exc}")
            raise

    # ── Core write helper ──

    def _execute_write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """Execute a write transaction with BEGIN IMMEDIATE and jitter retry.

        *fn* receives the connection and should perform INSERT/UPDATE/DELETE
        statements.  The caller must NOT call ``commit()`` — that's handled
        here after *fn* returns.

        BEGIN IMMEDIATE acquires the WAL write lock at transaction start
        (not at commit time), so lock contention surfaces immediately.
        On ``database is locked``, we release the Python lock, sleep a
        random 20-150ms, and retry — breaking the convoy pattern that
        SQLite's built-in deterministic backoff creates.

        Returns whatever *fn* returns.
        """
        last_err: Optional[Exception] = None
        for attempt in range(self._WRITE_MAX_RETRIES):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        result = fn(self._conn)
                        self._conn.commit()
                    except BaseException:
                        try:
                            self._conn.rollback()
                        except Exception:
                            pass
                        raise
                # Success — periodic best-effort checkpoint.
                self._write_count += 1
                if self._write_count % self._CHECKPOINT_EVERY_N_WRITES == 0:
                    self._try_wal_checkpoint()
                return result
            except sqlite3.OperationalError as exc:
                err_msg = str(exc).lower()
                if "locked" in err_msg or "busy" in err_msg:
                    last_err = exc
                    if attempt < self._WRITE_MAX_RETRIES - 1:
                        jitter = random.uniform(
                            self._WRITE_RETRY_MIN_S,
                            self._WRITE_RETRY_MAX_S,
                        )
                        time.sleep(jitter)
                        continue
                # Non-lock error or retries exhausted — propagate.
                raise
        # Retries exhausted (shouldn't normally reach here).
        raise last_err or sqlite3.OperationalError(
            "database is locked after max retries"
        )

    def _reclaim_freelist_on_startup(self) -> None:
        """启动时把删除留下的空闲页增量还给操作系统,防止 state.db 膨胀。

        仅对 ``auto_vacuum=INCREMENTAL`` 的库有效(NONE 模式下
        ``incremental_vacuum`` 是 no-op)。限制单次回收页数,避免积压很多时
        长时间卡住启动;日常空闲页很少,通常是毫秒级。
        """
        MIN_FREE_PAGES = 2560      # ~10MB 以下不值得回收
        MAX_PAGES = 50000          # 单次最多回收 ~200MB,避免积压时卡启动
        try:
            free_pages = int(self._conn.execute("PRAGMA freelist_count").fetchone()[0])
        except Exception:
            return
        if free_pages < MIN_FREE_PAGES:
            return
        pages = min(free_pages, MAX_PAGES)
        with self._lock:
            self._conn.execute(f"PRAGMA incremental_vacuum({pages})")
        logger.info(
            "state.db incremental_vacuum reclaimed up to %d free page(s) (freelist was %d)",
            pages, free_pages,
        )

    def _try_wal_checkpoint(self) -> None:
        """Best-effort TRUNCATE WAL checkpoint.  Never raises.

        Flushes committed WAL frames back into the main DB file and
        truncates the WAL file to zero bytes.  Keeps the WAL from
        growing unbounded when many processes hold persistent
        connections.

        PASSIVE checkpoint never truncates the WAL file; it leaves the file
        at its high-water mark until an explicit TRUNCATE checkpoint runs.
        This method is already off the hot write path and protected by
        ``self._lock``, so a short TRUNCATE checkpoint is the right tradeoff
        for long-lived desktop/gateway processes.
        """
        try:
            with self._lock:
                result = self._conn.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)"
                ).fetchone()
                if result and result[1] > 0:
                    logger.debug(
                        "WAL checkpoint: %d/%d pages checkpointed",
                        result[2], result[1],
                    )
        except Exception:
            pass  # Best effort — never fatal.

    def close(self):
        """Close the database connection.

        Attempts a TRUNCATE WAL checkpoint first so that exiting processes
        help shrink the WAL file.
        """
        with self._lock:
            if self._conn:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    pass
                self._conn.close()
                self._conn = None

    @staticmethod
    def _parse_schema_columns(schema_sql: str) -> Dict[str, Dict[str, str]]:
        """Extract expected columns per table from SCHEMA_SQL.

        Uses an in-memory SQLite database to parse the SQL — SQLite itself
        handles all syntax (DEFAULT expressions with commas, inline
        REFERENCES, CHECK constraints, etc.) so there are zero regex
        edge cases.  The in-memory DB is opened, the schema DDL is
        executed, and PRAGMA table_info extracts the column metadata.

        Adding a column to SCHEMA_SQL is all that's needed; the
        reconciliation loop picks it up automatically.
        """
        ref = sqlite3.connect(":memory:")
        try:
            ref.executescript(schema_sql)
            table_columns: Dict[str, Dict[str, str]] = {}
            for (tbl,) in ref.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall():
                cols: Dict[str, str] = {}
                for row in ref.execute(
                    f'PRAGMA table_info("{tbl}")'
                ).fetchall():
                    # row: (cid, name, type, notnull, dflt_value, pk)
                    col_name = row[1]
                    col_type = row[2] or ""
                    notnull = row[3]
                    default = row[4]
                    pk = row[5]
                    # Reconstruct the type expression for ALTER TABLE ADD COLUMN
                    parts = [col_type] if col_type else []
                    if notnull and not pk:
                        parts.append("NOT NULL")
                    if default is not None:
                        parts.append(f"DEFAULT {default}")
                    cols[col_name] = " ".join(parts)
                table_columns[tbl] = cols
            return table_columns
        finally:
            ref.close()

    def _reconcile_columns(self, cursor: sqlite3.Cursor) -> None:
        """Ensure live tables have every column declared in SCHEMA_SQL.

        Follows the Beets/sqlite-utils pattern: the CREATE TABLE definition
        in SCHEMA_SQL is the single source of truth for the desired schema.
        On every startup this method diffs the live columns (via PRAGMA
        table_info) against the declared columns, and ADDs any that are
        missing.

        This makes column additions a declarative operation — just add
        the column to SCHEMA_SQL and it appears on the next startup.
        Version-gated migration blocks are no longer needed for ADD COLUMN.
        """
        expected = self._parse_schema_columns(SCHEMA_SQL)
        for table_name, declared_cols in expected.items():
            # Get current columns from the live table
            try:
                rows = cursor.execute(
                    f'PRAGMA table_info("{table_name}")'
                ).fetchall()
            except sqlite3.OperationalError:
                continue  # Table doesn't exist yet (shouldn't happen after executescript)
            live_cols = set()
            for row in rows:
                # PRAGMA table_info returns (cid, name, type, notnull, dflt_value, pk)
                name = row[1] if isinstance(row, (tuple, list)) else row["name"]
                live_cols.add(name)

            for col_name, col_type in declared_cols.items():
                if col_name not in live_cols:
                    safe_name = col_name.replace('"', '""')
                    try:
                        cursor.execute(
                            f'ALTER TABLE "{table_name}" ADD COLUMN "{safe_name}" {col_type}'
                        )
                    except sqlite3.OperationalError as exc:
                        # Expected: "duplicate column name" from a race or
                        # re-run.  Unexpected: "Cannot add a NOT NULL column
                        # with default value NULL" from a schema mistake.
                        # Log at DEBUG so it's visible in agent.log.
                        logger.debug(
                            "reconcile %s.%s: %s", table_name, col_name, exc,
                        )

    def _reconcile_team_mission_node_primary_key(self, cursor: sqlite3.Cursor) -> None:
        """Ensure Team Mission nodes are keyed by mission and node.

        A Team Mission conversation can contain multiple missions, and planners
        may reuse local node ids such as ``root`` or ``worker`` in each mission.
        The durable identity is therefore ``(mission_id, node_id)``.  Older
        databases used a global ``node_id`` primary key, which made later
        missions overwrite earlier graph nodes.
        """
        try:
            rows = cursor.execute('PRAGMA table_info("team_mission_nodes")').fetchall()
        except sqlite3.OperationalError:
            return
        pk_columns = [
            (row["name"] if isinstance(row, sqlite3.Row) else row[1])
            for row in sorted(
                rows,
                key=lambda item: item["pk"] if isinstance(item, sqlite3.Row) else item[5],
            )
            if (row["pk"] if isinstance(row, sqlite3.Row) else row[5])
        ]
        if pk_columns == ["mission_id", "node_id"]:
            return

        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("DROP INDEX IF EXISTS idx_team_mission_nodes_mission")
        cursor.execute("ALTER TABLE team_mission_nodes RENAME TO team_mission_nodes_legacy_pk")
        cursor.execute(
            """
            CREATE TABLE team_mission_nodes (
                node_id TEXT NOT NULL,
                mission_id TEXT NOT NULL REFERENCES team_missions(mission_id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                objective TEXT,
                status TEXT NOT NULL,
                assignee_profile_id TEXT,
                assignee_profile_version_id TEXT,
                canonical_node_id TEXT,
                task_frame_id TEXT,
                runtime_stable_session_id TEXT,
                runtime_session_id TEXT,
                runtime_scope_key TEXT,
                output_contract_json TEXT,
                metadata_json TEXT,
                position_x REAL NOT NULL DEFAULT 0,
                position_y REAL NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (mission_id, node_id)
            )
            """
        )
        cursor.execute(
            """
            INSERT OR REPLACE INTO team_mission_nodes (
                node_id, mission_id, kind, title, objective, status,
                assignee_profile_id, assignee_profile_version_id,
                canonical_node_id, task_frame_id, runtime_stable_session_id,
                runtime_session_id, runtime_scope_key,
                output_contract_json, metadata_json, position_x, position_y,
                created_at, updated_at
            )
            SELECT
                node_id, mission_id, kind, title, objective, status,
                assignee_profile_id, assignee_profile_version_id,
                canonical_node_id, task_frame_id, runtime_stable_session_id,
                runtime_session_id, runtime_scope_key,
                output_contract_json, metadata_json, position_x, position_y,
                created_at, updated_at
            FROM team_mission_nodes_legacy_pk
            ORDER BY updated_at ASC, created_at ASC
            """
        )
        cursor.execute("DROP TABLE team_mission_nodes_legacy_pk")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_team_mission_nodes_mission "
            "ON team_mission_nodes(mission_id, created_at ASC)"
        )
        cursor.execute("PRAGMA foreign_keys=ON")

    def _backfill_session_list_summaries(self, cursor: sqlite3.Cursor) -> None:
        """Populate denormalized list fields from active message rows.

        This is a one-time compatibility path for databases created before
        ``sessions.preview`` and ``sessions.last_active`` existed. New writes
        keep these fields current, so list endpoints do not need to aggregate
        over the messages table on every sidebar refresh.
        """
        cursor.execute(
            """
            UPDATE sessions
            SET
                message_count = (
                    SELECT COUNT(1)
                    FROM messages m
                    WHERE m.session_id = sessions.id
                      AND m.active = 1
                ),
                preview = COALESCE((
                    SELECT CASE
                        WHEN LENGTH(raw.preview_raw) > 60 THEN SUBSTR(raw.preview_raw, 1, 60) || '...'
                        ELSE raw.preview_raw
                    END
                    FROM (
                        SELECT SUBSTR(REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' '), 1, 63) AS preview_raw
                        FROM messages m
                        WHERE m.session_id = sessions.id
                          AND m.active = 1
                          AND m.role = 'user'
                          AND m.content IS NOT NULL
                        ORDER BY m.timestamp, m.id
                        LIMIT 1
                    ) raw
                ), ''),
                last_active = (
                    SELECT MAX(m.timestamp)
                    FROM messages m
                    WHERE m.session_id = sessions.id
                      AND m.active = 1
                )
            """
        )
        rows = cursor.execute(
            """
            SELECT
                s.id,
                s.display_title_source,
                m.content AS first_user_content
            FROM sessions s
            LEFT JOIN messages m
              ON m.id = (
                  SELECT m2.id
                  FROM messages m2
                  WHERE m2.session_id = s.id
                    AND m2.active = 1
                    AND m2.role = 'user'
                    AND m2.content IS NOT NULL
                  ORDER BY m2.timestamp, m2.id
                  LIMIT 1
              )
            """
        ).fetchall()
        for row in rows:
            if str(row["display_title_source"] or "") == "user":
                continue
            display_title = self._message_display_title_text(row["first_user_content"])
            cursor.execute(
                """
                UPDATE sessions
                SET display_title = ?,
                    display_title_source = CASE
                        WHEN ? != '' THEN 'first_user_message'
                        ELSE ''
                    END
                WHERE id = ?
                """,
                (display_title, display_title, row["id"]),
            )

    def _migrate_agent_profile_versions_to_latest_profiles(self, cursor: sqlite3.Cursor) -> None:
        """Fold the removed profile version table into latest profile rows."""

        tables = cursor.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = 'agent_profile_versions'
            """
        ).fetchall()
        if not tables:
            return
        rows = cursor.execute(
            """
            SELECT *
            FROM agent_profile_versions
            ORDER BY agent_profile_id ASC, version_number DESC, published_at DESC, id ASC
            """
        ).fetchall()
        seen_profile_ids: set[str] = set()
        for version in rows:
            profile_id = str(version["agent_profile_id"] or "").strip()
            if not profile_id or profile_id in seen_profile_ids:
                continue
            seen_profile_ids.add(profile_id)
            profile = cursor.execute(
                "SELECT * FROM agent_profiles WHERE id = ?",
                (profile_id,),
            ).fetchone()
            if profile is None:
                continue
            cursor.execute(
                """
                UPDATE agent_profiles
                SET
                    name = COALESCE(NULLIF(?, ''), name),
                    avatar = COALESCE(NULLIF(?, ''), avatar),
                    description = COALESCE(NULLIF(?, ''), description),
                    category = COALESCE(NULLIF(?, ''), category),
                    tags_json = COALESCE(NULLIF(?, ''), tags_json),
                    default_model = COALESCE(NULLIF(?, ''), default_model),
                    default_provider = COALESCE(NULLIF(?, ''), default_provider),
                    default_permission_mode = COALESCE(NULLIF(?, ''), default_permission_mode),
                    default_toolsets_json = COALESCE(NULLIF(?, ''), default_toolsets_json),
                    recommended_skills_json = COALESCE(NULLIF(?, ''), recommended_skills_json),
                    current_version_id = COALESCE(NULLIF(?, ''), current_version_id),
                    current_version_number = CASE WHEN ? > 0 THEN ? ELSE current_version_number END,
                    updated_at = CASE WHEN ? > updated_at THEN ? ELSE updated_at END
                WHERE id = ?
                """,
                (
                    version["name"] or "",
                    version["avatar"] or "",
                    version["description"] or "",
                    version["category"] or "",
                    version["tags_json"] or "",
                    version["default_model"] or "",
                    version["default_provider"] or "",
                    version["default_permission_mode"] or "",
                    version["default_toolsets_json"] or "",
                    version["recommended_skills_json"] or "",
                    version["id"] or "",
                    int(version["version_number"] or 0),
                    int(version["version_number"] or 0),
                    float(version["published_at"] or 0),
                    float(version["published_at"] or 0),
                    profile_id,
                ),
            )
        cursor.execute("DROP TABLE IF EXISTS agent_profile_versions")

    def _init_schema(self):
        """Create tables and FTS if they don't exist, reconcile columns.

        Schema management follows the declarative reconciliation pattern
        (Beets, sqlite-utils): SCHEMA_SQL is the single source of truth.
        On existing databases, _reconcile_columns() diffs live columns
        against SCHEMA_SQL and ADDs any missing ones.  This eliminates
        the version-gated migration chain for column additions, making
        it impossible for reordered or inserted migrations to skip columns.

        The schema_version table is retained for future data migrations
        (transforming existing rows) which cannot be handled declaratively.
        """
        cursor = self._conn.cursor()

        cursor.executescript(SCHEMA_SQL)

        # ── Declarative column reconciliation ──────────────────────────
        # Diff live tables against SCHEMA_SQL and ADD any missing columns.
        # This is idempotent and self-healing: even if a version-gated
        # migration was skipped (e.g. due to version renumbering), the
        # column gets created here.
        self._reconcile_columns(cursor)
        self._reconcile_team_mission_node_primary_key(cursor)

        # Indexes that reference reconciler-added columns must be created
        # AFTER _reconcile_columns runs — declaring them in SCHEMA_SQL
        # makes the initial executescript fail on legacy DBs (the index's
        # WHERE clause references a column that doesn't exist yet).
        try:
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_platform_msg_id "
                "ON messages(session_id, platform_message_id) "
                "WHERE platform_message_id IS NOT NULL"
            )
        except sqlite3.OperationalError as exc:
            logger.debug("idx_messages_platform_msg_id create skipped: %s", exc)

        try:
            cursor.executescript(DEFERRED_INDEX_SQL)
        except sqlite3.OperationalError as exc:
            logger.debug("deferred message indexes create skipped: %s", exc)

        # ── Schema version bookkeeping ─────────────────────────────────
        # Bump to current so future data migrations (if any) can gate on
        # version.  No version-gated column additions remain.
        cursor.execute("SELECT version FROM schema_version LIMIT 1")
        row = cursor.fetchone()
        if row is None:
            self._backfill_session_list_summaries(cursor)
            cursor.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
        else:
            current_version = row["version"] if isinstance(row, sqlite3.Row) else row[0]
            # Data migrations that can't be expressed declaratively (row
            # backfills, index changes tied to a specific version step) stay
            # in a version-gated chain. Column additions are handled by
            # _reconcile_columns() above and no longer need entries here.
            if current_version < 10:
                # v10: trigram FTS5 table for CJK/substring search. The
                # virtual table + triggers are created unconditionally via
                # FTS_TRIGRAM_SQL below, but existing rows need a one-time
                # backfill into the FTS index.
                try:
                    cursor.execute("SELECT * FROM messages_fts_trigram LIMIT 0")
                    _fts_trigram_exists = True
                except sqlite3.OperationalError:
                    _fts_trigram_exists = False
                if not _fts_trigram_exists:
                    cursor.executescript(FTS_TRIGRAM_SQL)
                    cursor.execute(
                        "INSERT INTO messages_fts_trigram(rowid, content) "
                        "SELECT id, content FROM messages WHERE content IS NOT NULL"
                    )
            if current_version < 11:
                # v11: re-index FTS5 tables to cover tool_name + tool_calls and
                # switch from external-content to inline mode. Existing DBs have
                # old-schema FTS tables and triggers that IF NOT EXISTS won't
                # overwrite, so we drop them explicitly and let the post-migration
                # existence checks (below) recreate them from FTS_SQL /
                # FTS_TRIGRAM_SQL, then backfill every message row. Fixes #16751.
                for _trig in (
                    "messages_fts_insert",
                    "messages_fts_delete",
                    "messages_fts_update",
                    "messages_fts_trigram_insert",
                    "messages_fts_trigram_delete",
                    "messages_fts_trigram_update",
                ):
                    try:
                        cursor.execute(f"DROP TRIGGER IF EXISTS {_trig}")
                    except sqlite3.OperationalError:
                        pass
                for _tbl in ("messages_fts", "messages_fts_trigram"):
                    try:
                        cursor.execute(f"DROP TABLE IF EXISTS {_tbl}")
                    except sqlite3.OperationalError:
                        pass
                # Recreate virtual tables + triggers with the new inline-mode
                # schema that indexes content || tool_name || tool_calls.
                cursor.executescript(FTS_SQL)
                cursor.executescript(FTS_TRIGRAM_SQL)
                # Backfill both indexes from every existing messages row.
                cursor.execute(
                    "INSERT INTO messages_fts(rowid, content) "
                    "SELECT id, "
                    "COALESCE(content, '') || ' ' || "
                    "COALESCE(tool_name, '') || ' ' || "
                    "COALESCE(tool_calls, '') "
                    "FROM messages"
                )
                cursor.execute(
                    "INSERT INTO messages_fts_trigram(rowid, content) "
                    "SELECT id, "
                    "COALESCE(content, '') || ' ' || "
                    "COALESCE(tool_name, '') || ' ' || "
                    "COALESCE(tool_calls, '') "
                    "FROM messages"
                )
            if current_version < 14:
                try:
                    cursor.execute("UPDATE messages SET active = 1 WHERE active IS NULL")
                except sqlite3.OperationalError:
                    pass
            if current_version < 18:
                self._backfill_session_list_summaries(cursor)
            if current_version < 20:
                self._migrate_agent_profile_versions_to_latest_profiles(cursor)
            if current_version < SCHEMA_VERSION:
                cursor.execute(
                    "UPDATE schema_version SET version = ?",
                    (SCHEMA_VERSION,),
                )

        # Unique title index — always ensure it exists
        try:
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_title_unique "
                "ON sessions(title) WHERE title IS NOT NULL"
            )
        except sqlite3.OperationalError:
            pass  # Index already exists

        # FTS5 setup (separate because CREATE VIRTUAL TABLE can't be in executescript with IF NOT EXISTS reliably)
        try:
            cursor.execute("SELECT * FROM messages_fts LIMIT 0")
        except sqlite3.OperationalError:
            cursor.executescript(FTS_SQL)

        # Trigram FTS5 for CJK/substring search
        try:
            cursor.execute("SELECT * FROM messages_fts_trigram LIMIT 0")
        except sqlite3.OperationalError:
            cursor.executescript(FTS_TRIGRAM_SQL)

        self._conn.commit()

    # =========================================================================
    # Session lifecycle
    # =========================================================================

    def _insert_session_row(
        self,
        session_id: str,
        source: str,
        model: str = None,
        model_config: Dict[str, Any] = None,
        system_prompt: str = None,
        user_id: str = None,
        parent_session_id: str = None,
        transient: bool = False,
    ) -> None:
        """Shared INSERT OR IGNORE for session rows."""
        def _do(conn):
            conn.execute(
                """INSERT OR IGNORE INTO sessions (id, source, user_id, model, model_config,
                   system_prompt, parent_session_id, started_at, transient)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    source,
                    user_id,
                    model,
                    json.dumps(model_config) if model_config else None,
                    system_prompt,
                    parent_session_id,
                    time.time(),
                    1 if transient else 0,
                ),
            )
        self._execute_write(_do)

    def create_session(self, session_id: str, source: str, **kwargs) -> str:
        """Create a new session record. Returns the session_id."""
        self._insert_session_row(session_id, source, **kwargs)
        return session_id

    def end_session(self, session_id: str, end_reason: str) -> None:
        """Mark a session as ended.

        No-ops when the session is already ended. The first end_reason wins:
        compression-split sessions must keep their ``end_reason = 'compression'``
        record even if a later stale ``end_session()`` call (e.g. from a
        desynced CLI session_id after ``/resume`` or ``/branch``) targets them
        with a different reason. Use ``reopen_session()`` first if you
        intentionally need to re-end a closed session with a new reason.
        """
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = ? "
                "WHERE id = ? AND ended_at IS NULL",
                (time.time(), end_reason, session_id),
            )
        self._execute_write(_do)

    def reopen_session(self, session_id: str) -> None:
        """Clear ended_at/end_reason so a session can be resumed."""
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET ended_at = NULL, end_reason = NULL WHERE id = ?",
                (session_id,),
            )
        self._execute_write(_do)

    def repair_orphaned_foreign_key_rows(self) -> int:
        """Repair non-authoritative index/cache rows left dangling by old builds.

        The canonical owners are ``sessions``, ``team_missions`` and
        ``team_capability_snapshots``.  Lineage/idempotency/snapshot binding
        rows only index those owners, so deleting or orphaning invalid rows is
        the only valid recovery.  This keeps ``PRAGMA foreign_key_check`` clean
        without inventing placeholder parent records.
        """

        def affected(cursor: sqlite3.Cursor) -> int:
            return max(0, int(cursor.rowcount or 0))

        def _do(conn):
            repaired = 0
            repaired += affected(conn.execute(
                """
                DELETE FROM session_lineage
                WHERE NOT EXISTS (
                    SELECT 1 FROM sessions s WHERE s.id = session_lineage.session_id
                )
                """
            ))
            repaired += affected(conn.execute(
                """
                UPDATE session_lineage
                SET parent_session_id = NULL
                WHERE parent_session_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM sessions s WHERE s.id = session_lineage.parent_session_id
                  )
                """
            ))
            repaired += affected(conn.execute(
                """
                DELETE FROM session_branch_requests
                WHERE NOT EXISTS (
                    SELECT 1 FROM sessions s WHERE s.id = session_branch_requests.source_session_id
                )
                   OR NOT EXISTS (
                    SELECT 1 FROM sessions s WHERE s.id = session_branch_requests.result_session_id
                )
                """
            ))
            repaired += affected(conn.execute(
                """
                DELETE FROM team_capability_snapshot_bindings
                WHERE NOT EXISTS (
                    SELECT 1 FROM team_missions m
                    WHERE m.mission_id = team_capability_snapshot_bindings.mission_id
                )
                   OR NOT EXISTS (
                    SELECT 1 FROM team_capability_snapshots s
                    WHERE s.snapshot_id = team_capability_snapshot_bindings.snapshot_id
                )
                """
            ))
            return repaired

        return self._execute_write(_do)

    def update_system_prompt(self, session_id: str, system_prompt: str) -> None:
        """Store the full assembled system prompt snapshot."""
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET system_prompt = ? WHERE id = ?",
                (system_prompt, session_id),
            )
        self._execute_write(_do)

    def update_token_counts(
        self,
        session_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: Optional[float] = None,
        actual_cost_usd: Optional[float] = None,
        cost_status: Optional[str] = None,
        cost_source: Optional[str] = None,
        pricing_version: Optional[str] = None,
        billing_provider: Optional[str] = None,
        billing_base_url: Optional[str] = None,
        billing_mode: Optional[str] = None,
        api_call_count: int = 0,
        absolute: bool = False,
    ) -> None:
        """Update token counters and backfill model if not already set.

        When *absolute* is False (default), values are **incremented** — use
        this for per-API-call deltas (CLI path).

        When *absolute* is True, values are **set directly** — use this when
        the caller already holds cumulative totals (gateway path, where the
        cached agent accumulates across messages).
        """
        # Ensure the session row exists so the UPDATE doesn't silently affect
        # 0 rows.  Under concurrent load (cron + kanban + delegate_task) the
        # initial create_session() may have failed due to SQLite locking.
        # INSERT OR IGNORE is cheap and idempotent.
        self._insert_session_row(session_id, "unknown", model=model)
        if absolute:
            sql = """UPDATE sessions SET
                   input_tokens = ?,
                   output_tokens = ?,
                   cache_read_tokens = ?,
                   cache_write_tokens = ?,
                   reasoning_tokens = ?,
                   estimated_cost_usd = COALESCE(?, 0),
                   actual_cost_usd = CASE
                       WHEN ? IS NULL THEN actual_cost_usd
                       ELSE ?
                   END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?),
                   api_call_count = ?
                   WHERE id = ?"""
        else:
            sql = """UPDATE sessions SET
                   input_tokens = input_tokens + ?,
                   output_tokens = output_tokens + ?,
                   cache_read_tokens = cache_read_tokens + ?,
                   cache_write_tokens = cache_write_tokens + ?,
                   reasoning_tokens = reasoning_tokens + ?,
                   estimated_cost_usd = COALESCE(estimated_cost_usd, 0) + COALESCE(?, 0),
                   actual_cost_usd = CASE
                       WHEN ? IS NULL THEN actual_cost_usd
                       ELSE COALESCE(actual_cost_usd, 0) + ?
                   END,
                   cost_status = COALESCE(?, cost_status),
                   cost_source = COALESCE(?, cost_source),
                   pricing_version = COALESCE(?, pricing_version),
                   billing_provider = COALESCE(billing_provider, ?),
                   billing_base_url = COALESCE(billing_base_url, ?),
                   billing_mode = COALESCE(billing_mode, ?),
                   model = COALESCE(model, ?),
                   api_call_count = COALESCE(api_call_count, 0) + ?
                   WHERE id = ?"""
        params = (
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
            reasoning_tokens,
            estimated_cost_usd,
            actual_cost_usd,
            actual_cost_usd,
            cost_status,
            cost_source,
            pricing_version,
            billing_provider,
            billing_base_url,
            billing_mode,
            model,
            api_call_count,
            session_id,
        )
        def _do(conn):
            conn.execute(sql, params)
        self._execute_write(_do)

    def ensure_session(
        self,
        session_id: str,
        source: str = "unknown",
        model: str = None,
        **kwargs,
    ) -> str:
        """Ensure a session row exists (INSERT OR IGNORE). Accepts optional kwargs."""
        self._insert_session_row(session_id, source, model=model, **kwargs)
        return session_id

    def prune_empty_ghost_sessions(self, sessions_dir: "Optional[Path]" = None) -> int:
        """Remove empty TUI ghost sessions (no messages, no title, >24hr old)."""
        cutoff = time.time() - 86400  # Only sessions older than 24 hours

        def _do(conn):
            rows = conn.execute("""
                SELECT id FROM sessions
                WHERE source = 'tui'
                  AND title IS NULL
                  AND ended_at IS NOT NULL
                  AND started_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM messages WHERE messages.session_id = sessions.id
                  )
            """, (cutoff,)).fetchall()
            ids = [r[0] if isinstance(r, (tuple, list)) else r["id"] for r in rows]
            if ids:
                placeholders = ",".join("?" * len(ids))
                conn.execute(
                    f"DELETE FROM sessions WHERE id IN ({placeholders})", ids
                )
            return ids

        removed_ids = self._execute_write(_do) or []
        # Clean up any on-disk session files (belt-and-suspenders)
        if sessions_dir and removed_ids:
            for sid in removed_ids:
                self._remove_session_files(sessions_dir, sid)
        return len(removed_ids)

    def finalize_orphaned_compression_sessions(self) -> int:
        """Mark orphaned compression continuation sessions as ended.

        Targets child sessions that were never finalized: parent is ended
        with reason='compression', child has messages but no end_reason/ended_at
        and api_call_count=0.  Non-destructive: preserves all messages and sets
        end_reason='orphaned_compression'.  Fix for #20001.
        """
        cutoff = time.time() - 604800  # 7 days

        def _do(conn):
            now = time.time()
            result = conn.execute(
                """
                UPDATE sessions
                SET ended_at = ?,
                    end_reason = 'orphaned_compression'
                WHERE api_call_count = 0
                  AND end_reason IS NULL
                  AND ended_at IS NULL
                  AND started_at < ?
                  AND parent_session_id IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM sessions p
                      WHERE p.id = sessions.parent_session_id
                        AND p.end_reason = 'compression'
                        AND p.ended_at IS NOT NULL
                  )
                  AND EXISTS (
                      SELECT 1 FROM messages m
                      WHERE m.session_id = sessions.id
                  )
                """,
                (now, cutoff),
            )
            return result.rowcount

        return self._execute_write(_do) or 0

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get a session by ID."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def resolve_session_id(self, session_id_or_prefix: str) -> Optional[str]:
        """Resolve an exact or uniquely prefixed session ID to the full ID.

        Returns the exact ID when it exists. Otherwise treats the input as a
        prefix and returns the single matching session ID if the prefix is
        unambiguous. Returns None for no matches or ambiguous prefixes.
        """
        exact = self.get_session(session_id_or_prefix)
        if exact:
            return exact["id"]

        escaped = (
            session_id_or_prefix
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id FROM sessions WHERE id LIKE ? ESCAPE '\\' ORDER BY started_at DESC LIMIT 2",
                (f"{escaped}%",),
            )
            matches = [row["id"] for row in cursor.fetchall()]
        if len(matches) == 1:
            return matches[0]
        return None

    # Maximum length for session titles
    MAX_TITLE_LENGTH = 100

    @staticmethod
    def sanitize_title(title: Optional[str]) -> Optional[str]:
        """Validate and sanitize a session title.

        - Strips leading/trailing whitespace
        - Removes ASCII control characters (0x00-0x1F, 0x7F) and problematic
          Unicode control chars (zero-width, RTL/LTR overrides, etc.)
        - Collapses internal whitespace runs to single spaces
        - Normalizes empty/whitespace-only strings to None
        - Enforces MAX_TITLE_LENGTH

        Returns the cleaned title string or None.
        Raises ValueError if the title exceeds MAX_TITLE_LENGTH after cleaning.
        """
        if not title:
            return None

        # Remove ASCII control characters (0x00-0x1F, 0x7F) but keep
        # whitespace chars (\t=0x09, \n=0x0A, \r=0x0D) so they can be
        # normalized to spaces by the whitespace collapsing step below
        cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', title)

        # Remove problematic Unicode control characters:
        # - Zero-width chars (U+200B-U+200F, U+FEFF)
        # - Directional overrides (U+202A-U+202E, U+2066-U+2069)
        # - Object replacement (U+FFFC), interlinear annotation (U+FFF9-U+FFFB)
        cleaned = re.sub(
            r'[\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff\ufffc\ufff9-\ufffb]',
            '', cleaned,
        )

        # Collapse internal whitespace runs and strip
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()

        if not cleaned:
            return None

        if len(cleaned) > SessionDB.MAX_TITLE_LENGTH:
            raise ValueError(
                f"Title too long ({len(cleaned)} chars, max {SessionDB.MAX_TITLE_LENGTH})"
            )

        return cleaned

    def set_session_title(self, session_id: str, title: str, *, title_source: str = "user") -> bool:
        """Set or update a session's title.

        Returns True if session was found and title was set.
        Raises ValueError if title is already in use by another session,
        or if the title fails validation (too long, invalid characters).
        Empty/whitespace-only strings are normalized to None (clearing the title).

        ``title`` remains the legacy unique Hermes title used by CLI resume and
        platform integrations. ``display_title`` is the non-unique product title
        Dovie shows in history. Auto-generated summary titles are no longer a
        valid write source; ``title_source="auto"`` is ignored so canonical
        titles cannot regress behind first-user-message display titles.
        """
        normalized_source = str(title_source or "user").strip().lower() or "user"
        if normalized_source == "auto":
            return False
        title = self.sanitize_title(title)
        def _do(conn):
            if title:
                # Check uniqueness (allow the same session to keep its own title)
                cursor = conn.execute(
                    "SELECT id FROM sessions WHERE title = ? AND id != ?",
                    (title, session_id),
                )
                conflict = cursor.fetchone()
                if conflict:
                    raise ValueError(
                        f"Title '{title}' is already in use by session {conflict['id']}"
                    )
            cursor = conn.execute(
                """
                UPDATE sessions
                SET title = ?,
                    display_title = COALESCE(?, ''),
                    display_title_source = ?
                WHERE id = ?
                """,
                (title, title or "", normalized_source, session_id),
            )
            return cursor.rowcount
        rowcount = self._execute_write(_do)
        return rowcount > 0

    def get_session_title(self, session_id: str) -> Optional[str]:
        """Get the title for a session, or None."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT title FROM sessions WHERE id = ?", (session_id,)
            )
            row = cursor.fetchone()
        return row["title"] if row else None

    def get_session_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        """Look up a session by exact title. Returns session dict or None."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM sessions WHERE title = ?", (title,)
            )
            row = cursor.fetchone()
        return dict(row) if row else None

    def resolve_session_by_title(self, title: str) -> Optional[str]:
        """Resolve a title to a session ID, preferring the latest in a lineage.

        If the exact title exists, returns that session's ID.
        If not, searches for "title #N" variants and returns the latest one.
        If the exact title exists AND numbered variants exist, returns the
        latest numbered variant (the most recent continuation).
        """
        # First try exact match
        exact = self.get_session_by_title(title)

        # Also search for numbered variants: "title #2", "title #3", etc.
        # Escape SQL LIKE wildcards (%, _) in the title to prevent false matches
        escaped = title.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            cursor = self._conn.execute(
                "SELECT id, title, started_at FROM sessions "
                "WHERE title LIKE ? ESCAPE '\\' ORDER BY started_at DESC",
                (f"{escaped} #%",),
            )
            numbered = cursor.fetchall()

        if numbered:
            # Return the most recent numbered variant
            return numbered[0]["id"]
        elif exact:
            return exact["id"]
        return None

    def get_next_title_in_lineage(self, base_title: str) -> str:
        """Generate the next title in a lineage (e.g., "my session" → "my session #2").

        Strips any existing " #N" suffix to find the base name, then finds
        the highest existing number and increments.
        """
        # Strip existing #N suffix to find the true base
        match = re.match(r'^(.*?) #(\d+)$', base_title)
        if match:
            base = match.group(1)
        else:
            base = base_title

        # Find all existing numbered variants
        # Escape SQL LIKE wildcards (%, _) in the base to prevent false matches
        escaped = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            cursor = self._conn.execute(
                "SELECT title FROM sessions WHERE title = ? OR title LIKE ? ESCAPE '\\'",
                (base, f"{escaped} #%"),
            )
            existing = [row["title"] for row in cursor.fetchall()]

        if not existing:
            return base  # No conflict, use the base name as-is

        # Find the highest number
        max_num = 1  # The unnumbered original counts as #1
        for t in existing:
            m = re.match(r'^.* #(\d+)$', t)
            if m:
                max_num = max(max_num, int(m.group(1)))

        return f"{base} #{max_num + 1}"

    def get_compression_tip(self, session_id: str) -> Optional[str]:
        """Walk the compression-continuation chain forward and return the tip.

        A compression continuation is a child session where:
        1. The parent's ``end_reason = 'compression'``
        2. The child was created AFTER the parent was ended (started_at >= ended_at)

        The second condition distinguishes compression continuations from
        delegate subagents or branch children, which can also have a
        ``parent_session_id`` but were created while the parent was still live.

        Returns the session_id of the latest continuation in the chain, or the
        input ``session_id`` if it isn't part of a compression chain (or if the
        input itself doesn't exist).
        """
        current = session_id
        # Bound the walk defensively — compression chains this deep are
        # pathological and shouldn't happen in practice. 100 = plenty.
        for _ in range(100):
            with self._lock:
                cursor = self._conn.execute(
                    "SELECT id FROM sessions "
                    "WHERE parent_session_id = ? "
                    "  AND started_at >= ("
                    "      SELECT ended_at FROM sessions "
                    "      WHERE id = ? AND end_reason = 'compression'"
                    "  ) "
                    "ORDER BY started_at DESC LIMIT 1",
                    (current, current),
                )
                row = cursor.fetchone()
            if row is None:
                return current
            current = row["id"]
        return current

    def list_sessions_rich(
        self,
        source: str = None,
        exclude_sources: List[str] = None,
        limit: int = 20,
        offset: int = 0,
        include_children: bool = False,
        project_compression_tips: bool = True,
        order_by_last_active: bool = False,
        page_cursor: Optional[Dict[str, Any]] = None,
        id_query: str = None,
    ) -> List[Dict[str, Any]]:
        """List sessions with preview (first user message) and last active timestamp.

        Returns dicts with keys: id, source, model, title, started_at, ended_at,
        message_count, preview (first 60 chars of first user message),
        last_active (timestamp of last message).

        Reads denormalized list fields maintained on the ``sessions`` row;
        transcript bodies are loaded only by detail/history APIs.

        By default, child sessions (subagent runs, compression continuations)
        are excluded.  Pass ``include_children=True`` to include them.

        With ``project_compression_tips=True`` (default), sessions that are
        roots of compression chains are projected forward to their latest
        continuation — one logical conversation = one list entry, showing the
        live continuation's id/message_count/title/last_active. This prevents
        compressed continuations from being invisible to users while keeping
        delegate subagents and branches hidden. Pass ``False`` to return the
        raw root rows (useful for admin/debug UIs).

        Pass ``order_by_last_active=True`` to sort by most-recent activity
        instead of original conversation start time. For compression chains,
        the "most-recent activity" is taken from the live tip (not the root),
        so an old conversation that was compressed and continued recently
        surfaces in the correct slot. Ordering is computed at SQL level via
        a recursive CTE that walks compression-continuation edges, so LIMIT
        and OFFSET still apply efficiently.

        ``page_cursor`` is a keyset cursor emitted on each returned row as
        ``_page_cursor``. It keeps pagination stable while conversations are
        sorted by ``effective_last_active DESC, started_at DESC, id DESC``.
        """
        where_clauses = []
        params = []

        if not include_children:
            # Show root sessions and explicit user branches, while still
            # hiding sub-agent runs and compression continuations. Modern
            # non-destructive branches live in session_lineage and do not use
            # sessions.parent_session_id for transcript replay. The legacy
            # end_reason='branched' predicate is retained for old CLI rows.
            where_clauses.append(
                "(s.parent_session_id IS NULL"
                " OR EXISTS (SELECT 1 FROM session_lineage l"
                "            WHERE l.session_id = s.id"
                "            AND l.branch_origin = 'user_message_action')"
                " OR EXISTS (SELECT 1 FROM sessions p"
                "            WHERE p.id = s.parent_session_id"
                "            AND p.end_reason = 'branched'"
                "            AND s.started_at >= p.ended_at))"
            )

        if source:
            where_clauses.append("s.source = ?")
            params.append(source)
        if exclude_sources:
            placeholders = ",".join("?" for _ in exclude_sources)
            where_clauses.append(f"s.source NOT IN ({placeholders})")
            params.extend(exclude_sources)

        id_needle = (id_query or "").strip().lower()
        id_like_pattern = (
            "%"
            + id_needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
            if id_needle
            else ""
        )

        def _cursor_number(key: str) -> float:
            if not page_cursor:
                return 0.0
            try:
                return float(page_cursor.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0

        def _cursor_id() -> str:
            if not page_cursor:
                return ""
            value = page_cursor.get("id")
            return str(value) if value is not None else ""

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        if order_by_last_active:
            outer_where_clauses = list(where_clauses)
            outer_params = list(params)
            cursor_id = _cursor_id()
            if cursor_id:
                effective_last_active_expr = "COALESCE(cm.effective_last_active, COALESCE(s.last_active, s.started_at))"
                cursor_effective_last_active = _cursor_number("effective_last_active")
                cursor_started_at = _cursor_number("started_at")
                outer_where_clauses.append(
                    f"""(
                        {effective_last_active_expr} < ?
                        OR ({effective_last_active_expr} = ? AND s.started_at < ?)
                        OR ({effective_last_active_expr} = ? AND s.started_at = ? AND s.id < ?)
                    )"""
                )
                outer_params.extend(
                    [
                        cursor_effective_last_active,
                        cursor_effective_last_active,
                        cursor_started_at,
                        cursor_effective_last_active,
                        cursor_started_at,
                        cursor_id,
                    ]
                )
            if id_needle:
                outer_where_clauses.append(
                    "EXISTS (SELECT 1 FROM chain cq "
                    "WHERE cq.root_id = s.id "
                    "AND LOWER(cq.cur_id) LIKE ? ESCAPE '\\')"
                )
                outer_params.append(id_like_pattern)
            outer_where_sql = (
                f"WHERE {' AND '.join(outer_where_clauses)}"
                if outer_where_clauses
                else ""
            )
            # Compute effective_last_active by walking each surfaced session's
            # compression-continuation chain forward in SQL and taking the MAX
            # denormalized session activity timestamp across the chain. This
            # keeps ORDER BY + LIMIT in SQL without aggregating transcript rows,
            # while still surfacing old compression roots whose live tip is fresh.
            #
            # The CTE seeds from rows the outer WHERE admits (roots + branch
            # children), then recursively joins forward through
            # compression-continuation edges using the same criteria as
            # get_compression_tip (parent.end_reason='compression' AND
            # child.started_at >= parent.ended_at).
            query = f"""
                WITH RECURSIVE chain(root_id, cur_id) AS (
                    SELECT s.id, s.id FROM sessions s {where_sql}
                    UNION ALL
                    SELECT c.root_id, child.id
                    FROM chain c
                    JOIN sessions parent ON parent.id = c.cur_id
                    JOIN sessions child ON child.parent_session_id = c.cur_id
                    WHERE parent.end_reason = 'compression'
                      AND child.started_at >= parent.ended_at
                ),
                chain_max AS (
                    SELECT
                        root_id,
                        MAX(COALESCE(ss.last_active, ss.started_at)) AS effective_last_active
                    FROM chain c
                    JOIN sessions ss ON ss.id = c.cur_id
                    GROUP BY root_id
                )
                SELECT s.*,
                    COALESCE(s.preview, '') AS _preview_summary,
                    COALESCE(s.last_active, s.started_at) AS _last_active_summary,
                    COALESCE(cm.effective_last_active, COALESCE(s.last_active, s.started_at)) AS _effective_last_active
                FROM sessions s
                LEFT JOIN chain_max cm ON cm.root_id = s.id
                {outer_where_sql}
                ORDER BY _effective_last_active DESC, s.started_at DESC, s.id DESC
                LIMIT ? OFFSET ?
            """
            # WHERE params apply twice (CTE seed + outer select).
            params = params + outer_params + [limit, offset]
        else:
            outer_where_clauses = list(where_clauses)
            outer_params = list(params)
            cursor_id = _cursor_id()
            if cursor_id:
                cursor_started_at = _cursor_number("started_at")
                outer_where_clauses.append(
                    "(s.started_at < ? OR (s.started_at = ? AND s.id < ?))"
                )
                outer_params.extend([cursor_started_at, cursor_started_at, cursor_id])
            if id_needle:
                outer_where_clauses.append("LOWER(s.id) LIKE ? ESCAPE '\\'")
                outer_params.append(id_like_pattern)
            outer_where_sql = (
                f"WHERE {' AND '.join(outer_where_clauses)}"
                if outer_where_clauses
                else ""
            )
            query = f"""
                SELECT s.*,
                    COALESCE(s.preview, '') AS _preview_summary,
                    COALESCE(s.last_active, s.started_at) AS _last_active_summary
                FROM sessions s
                {outer_where_sql}
                ORDER BY s.started_at DESC, s.id DESC
                LIMIT ? OFFSET ?
            """
            params = outer_params + [limit, offset]
        with self._lock:
            cursor = self._conn.execute(query, params)
            rows = cursor.fetchall()
        sessions = []
        for row in rows:
            s = dict(row)
            s["preview"] = str(s.pop("_preview_summary", s.get("preview") or "") or "")
            last_active = s.pop("_last_active_summary", None)
            if last_active is not None:
                s["last_active"] = last_active
            effective_last_active = s.pop("_effective_last_active", None)
            if effective_last_active is None:
                effective_last_active = s.get("last_active") or s.get("started_at") or 0
            s["_page_cursor"] = {
                "effective_last_active": effective_last_active,
                "started_at": s.get("started_at") or 0,
                "id": s.get("id") or "",
            }
            sessions.append(s)

        # Project compression roots forward to their tips. Each row whose
        # end_reason is 'compression' has a continuation child; replace the
        # surfaced fields (id, message_count, title, last_active, ended_at,
        # end_reason, preview) with the tip's values so the list entry acts
        # as the live conversation. Keep the root's started_at to preserve
        # chronological ordering by original conversation start.
        if project_compression_tips and not include_children:
            projected = []
            for s in sessions:
                if s.get("end_reason") != "compression":
                    projected.append(s)
                    continue
                tip_id = self.get_compression_tip(s["id"])
                if tip_id == s["id"]:
                    projected.append(s)
                    continue
                tip_row = self._get_session_rich_row(tip_id)
                if not tip_row:
                    projected.append(s)
                    continue
                # Preserve the root's started_at for stable sort order, but
                # surface the tip's identity and activity data.
                merged = dict(s)
                for key in (
                    "id", "ended_at", "end_reason", "message_count",
                    "tool_call_count", "title", "display_title",
                    "display_title_source", "last_active", "preview", "model",
                    "system_prompt",
                ):
                    if key in tip_row:
                        merged[key] = tip_row[key]
                merged["_lineage_root_id"] = s["id"]
                projected.append(merged)
            sessions = projected

        return sessions

    # ------------------------------------------------------------------
    # Control-plane session_index (write-time projection; single-query read)
    # ------------------------------------------------------------------
    _SESSION_INDEX_COLUMNS = (
        "session_id", "owner_agent_profile_id", "owner_profile_version_id",
        "runtime_scope_key", "title", "preview", "source", "transient",
        "session_kind", "status", "running", "waiting_approval", "active_run_id",
        "active_runtime_session_id", "pending_approval_count", "team_id",
        "mission_id", "conversation_id", "message_count", "started_at",
        "updated_at", "last_activity",
    )

    @staticmethod
    def _session_index_row_to_item(row: sqlite3.Row) -> Dict[str, Any]:
        item = {key: row[key] for key in row.keys()}
        for flag in ("transient", "running", "waiting_approval"):
            item[flag] = bool(item.get(flag))
        item["_page_cursor"] = {
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "session_id": row["session_id"],
        }
        return item

    def upsert_session_index(
        self,
        *,
        session_id: str,
        owner_agent_profile_id: str = "",
        owner_profile_version_id: str = "",
        runtime_scope_key: str = "",
        title: str = "",
        preview: str = "",
        source: str = "unknown",
        transient: bool = False,
        session_kind: str = "hermes_session",
        status: str = "idle",
        running: bool = False,
        waiting_approval: bool = False,
        active_run_id: str = "",
        active_runtime_session_id: str = "",
        pending_approval_count: int = 0,
        team_id: str = "",
        mission_id: str = "",
        conversation_id: str = "",
        message_count: int = 0,
        started_at: Optional[float] = None,
        updated_at: Optional[float] = None,
        last_activity: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Full upsert of a control-plane session_index row (idempotent by id)."""
        sid = str(session_id or "").strip()
        if not sid:
            raise ValueError("session_id required for upsert_session_index")
        now = time.time()
        started = float(started_at if started_at is not None else now)
        updated = float(updated_at if updated_at is not None else now)
        values = {
            "session_id": sid,
            "owner_agent_profile_id": str(owner_agent_profile_id or ""),
            "owner_profile_version_id": str(owner_profile_version_id or ""),
            "runtime_scope_key": str(runtime_scope_key or ""),
            "title": str(title or ""),
            "preview": str(preview or ""),
            "source": str(source or "unknown"),
            "transient": 1 if transient else 0,
            "session_kind": str(session_kind or "hermes_session"),
            "status": str(status or "idle"),
            "running": 1 if running else 0,
            "waiting_approval": 1 if waiting_approval else 0,
            "active_run_id": str(active_run_id or ""),
            "active_runtime_session_id": str(active_runtime_session_id or ""),
            "pending_approval_count": int(pending_approval_count or 0),
            "team_id": str(team_id or ""),
            "mission_id": str(mission_id or ""),
            "conversation_id": str(conversation_id or ""),
            "message_count": int(message_count or 0),
            "started_at": started,
            "updated_at": updated,
            "last_activity": last_activity,
        }
        cols = list(values.keys())
        placeholders = ", ".join(f":{c}" for c in cols)
        update_cols = [c for c in cols if c != "session_id"]
        set_clause = ", ".join(f"{c}=excluded.{c}" for c in update_cols)

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            conn.execute(
                f"INSERT INTO session_index ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(session_id) DO UPDATE SET {set_clause}",
                values,
            )
            return values

        return self._execute_write(_do)

    def delete_session_index(self, session_id: str) -> int:
        sid = str(session_id or "").strip()
        if not sid:
            return 0

        def _do(conn: sqlite3.Connection) -> int:
            return int(conn.execute(
                "DELETE FROM session_index WHERE session_id = ?", (sid,)
            ).rowcount or 0)

        return self._execute_write(_do)

    def list_session_index(
        self,
        *,
        limit: int = 200,
        cursor: Optional[Dict[str, Any]] = None,
        include_transient: bool = False,
    ) -> Dict[str, Any]:
        """Single indexed read for the sidebar: keyset-paginated, newest first.

        No recursive CTE, no live merge, no per-session approval lookup, no
        per-profile fan-out — the status fields are already projected at write
        time. Ordering: updated_at DESC, started_at DESC, session_id DESC.
        """
        capped = max(1, min(int(limit or 200), 200))
        where = []
        params: List[Any] = []
        if not include_transient:
            where.append("transient = 0")
        if isinstance(cursor, dict) and cursor.get("session_id"):
            cu = float(cursor.get("updated_at") or 0)
            cs = float(cursor.get("started_at") or 0)
            ci = str(cursor.get("session_id") or "")
            where.append(
                "(updated_at < ? OR (updated_at = ? AND started_at < ?) "
                "OR (updated_at = ? AND started_at = ? AND session_id < ?))"
            )
            params.extend([cu, cu, cs, cu, cs, ci])
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        sql = (
            "SELECT * FROM session_index" + where_sql +
            " ORDER BY updated_at DESC, started_at DESC, session_id DESC LIMIT ?"
        )
        params.append(capped + 1)
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        has_more = len(rows) > capped
        page = rows[:capped]
        items = [self._session_index_row_to_item(r) for r in page]
        next_cursor = items[-1]["_page_cursor"] if (has_more and items) else None
        return {
            "sessions": items,
            "pageInfo": {"hasMore": has_more, "nextCursor": next_cursor},
        }

    def reconcile_session_index(
        self,
        *,
        exclude_sources: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Backfill/repair the index from the source of truth (sessions table).

        Upserts the static/display fields for every non-excluded session,
        preserving any live status fields already projected by write-time hooks
        (only inserts defaults for brand-new rows). Safe to run on startup and
        periodically; the index is always rebuildable from this.
        """
        excluded = tuple(exclude_sources if exclude_sources is not None else ("tool", "cron"))
        placeholders = ", ".join("?" for _ in excluded) if excluded else ""
        # Team-mission member-node runtime sessions (id like "team:...:node:...")
        # are data plane, never user-facing — they must not surface in the sidebar.
        # Mirrors the frontend isTeamMissionInternalRuntimeSessionId rule. Their
        # delegate_task / sub-agent children carry their OWN fresh id (the member
        # node session is their parent_session_id) and must be excluded too, or
        # every team task that runs delegate_task leaks worker chatter into the
        # sidebar as unattributed `tui` sessions (parent's node session never
        # surfaces, the child does — confusing the user with "Get latest GitHub
        # stats" / empty "新会话" rows that don't belong to any conversation).
        team_internal_clause = (
            "NOT (id LIKE 'team:%' AND id LIKE '%:node:%') "
            "AND NOT (COALESCE(parent_session_id,'') LIKE 'team:%:node:%')"
        )
        where = [team_internal_clause]
        params: List[Any] = []
        if excluded:
            where.append(f"COALESCE(source,'') NOT IN ({placeholders})")
            params.extend(excluded)
        select_sql = (
            "SELECT id, source, title, display_title, preview, started_at, "
            "last_active, message_count, transient FROM sessions WHERE "
            + " AND ".join(where)
        )

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            # Purge any team-internal node sessions that a prior reconcile leaked,
            # plus their delegate_task / sub-agent children (id is a fresh tui id
            # whose parent_session_id points at the team node session).
            conn.execute(
                "DELETE FROM session_index "
                "WHERE session_id LIKE 'team:%' AND session_id LIKE '%:node:%'"
            )
            conn.execute(
                "DELETE FROM session_index "
                "WHERE session_id IN ("
                " SELECT id FROM sessions "
                " WHERE COALESCE(parent_session_id,'') LIKE 'team:%:node:%'"
                ")"
            )
            rows = conn.execute(select_sql, tuple(params)).fetchall()
            upserted = 0
            for row in rows:
                started = float(row["started_at"] or 0)
                updated = float(row["last_active"] or row["started_at"] or 0)
                title = str(row["display_title"] or row["title"] or "")
                # Insert defaults for new rows; on conflict refresh only the
                # static/display fields, never the live status projection.
                conn.execute(
                    """
                    INSERT INTO session_index (
                        session_id, title, preview, source, transient,
                        message_count, started_at, updated_at, last_activity
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        title=excluded.title,
                        preview=excluded.preview,
                        source=excluded.source,
                        transient=excluded.transient,
                        message_count=excluded.message_count
                    """,
                    (
                        str(row["id"]),
                        title,
                        str(row["preview"] or ""),
                        str(row["source"] or "unknown"),
                        1 if row["transient"] else 0,
                        int(row["message_count"] or 0),
                        started,
                        updated,
                        updated,
                    ),
                )
                upserted += 1
            # Heal team-mission conversation rows whose mission is already terminal
            # but whose status projection is still "running"/waiting (e.g. a cancel
            # that bypassed the graph reducer) — otherwise the sidebar shows a
            # finished team task as running after restart.
            conn.execute(
                """
                UPDATE session_index
                   SET running = 0, status = 'idle', waiting_approval = 0,
                       active_run_id = '', active_runtime_session_id = '',
                       pending_approval_count = 0
                 WHERE session_kind = 'team_mission'
                   AND (running = 1 OR waiting_approval = 1 OR status != 'idle'
                        OR active_run_id != '' OR active_runtime_session_id != '')
                   AND mission_id IN (
                       SELECT mission_id FROM team_missions
                        WHERE LOWER(COALESCE(status,'')) IN
                              ('completed','failed','cancelled','canceled','interrupted')
                   )
                """
            )
            # Heal sessions (regular OR team_mission) whose active_run_id points
            # at a run that is already terminal in the runs table but whose
            # session_index row still says running=1. Pre-existing fix only
            # targeted non-team_mission rows; observed DB had a team_mission row
            # stuck running=1 + active_run_id=<completed-run> because the
            # mission was still in 'draft' (so the mission-status heal above did
            # not match) and some path skipped the run-write projection on the
            # leader's conversation run ending. Resolving this here is safe and
            # idempotent: it only touches rows whose active_run_id is verifiably
            # terminal in the runs table.
            conn.execute(
                """
                UPDATE session_index
                   SET running = 0, status = 'idle',
                       active_run_id = '', active_runtime_session_id = ''
                 WHERE running = 1
                   AND active_run_id != ''
                   AND active_run_id IN (
                       SELECT run_id FROM runs
                        WHERE LOWER(COALESCE(status,'')) IN
                              ('completed','failed','cancelled','canceled','interrupted')
                   )
                """
            )
            return {"reconciled": upserted}

        return self._execute_write(_do)

    def _get_session_rich_row(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single session with the same enriched columns as
        ``list_sessions_rich`` (preview + last_active). Returns None if the
        session doesn't exist.
        """
        query = """
            SELECT s.*,
                COALESCE(s.preview, '') AS _preview_summary,
                COALESCE(s.last_active, s.started_at) AS _last_active_summary
            FROM sessions s
            WHERE s.id = ?
        """
        with self._lock:
            cursor = self._conn.execute(query, (session_id,))
            row = cursor.fetchone()
        if not row:
            return None
        s = dict(row)
        s["preview"] = str(s.pop("_preview_summary", s.get("preview") or "") or "")
        s["last_active"] = s.pop("_last_active_summary", s.get("last_active") or s.get("started_at") or 0)
        return s

    # =========================================================================
    # Message storage
    # =========================================================================

    # Sentinel prefix used to distinguish JSON-encoded structured content
    # (multimodal messages: lists of parts like text + image_url) from plain
    # string content. The NUL byte is not legal in normal text, so this
    # cannot collide with real user content.
    _CONTENT_JSON_PREFIX = "\x00json:"

    @classmethod
    def _encode_content(cls, content: Any) -> Any:
        """Serialize structured (list/dict) message content for sqlite.

        sqlite3 can only bind ``str``, ``bytes``, ``int``, ``float``, and ``None``
        to query parameters. Multimodal messages have ``content`` as a list of
        parts (``[{"type": "text", ...}, {"type": "image_url", ...}]``), which
        raises ``ProgrammingError: Error binding parameter N: type 'list' is
        not supported`` when bound directly.

        Returns the value unchanged when it's already a safe scalar, or a
        sentinel-prefixed JSON string for lists/dicts. Paired with
        :meth:`_decode_content` on read.
        """
        if content is None or isinstance(content, (str, bytes, int, float)):
            return content
        try:
            return cls._CONTENT_JSON_PREFIX + json.dumps(content)
        except (TypeError, ValueError):
            # Last-resort fallback: stringify so persistence never fails.
            return str(content)

    @classmethod
    def _decode_content(cls, content: Any) -> Any:
        """Reverse :meth:`_encode_content`; returns scalars unchanged."""
        if isinstance(content, str) and content.startswith(cls._CONTENT_JSON_PREFIX):
            try:
                return json.loads(content[len(cls._CONTENT_JSON_PREFIX):])
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "Failed to decode JSON-encoded message content; "
                    "returning raw string"
                )
                return content
        return content

    @classmethod
    def _message_preview_text(cls, content: Any, limit: int = 60) -> str:
        """Return the compact user-facing preview stored on ``sessions``."""
        decoded = cls._decode_content(content)
        if isinstance(decoded, list):
            parts: list[str] = []
            for item in decoded:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
                else:
                    parts.append(str(item or ""))
            preview = " ".join(part for part in parts if part).strip()
            if not preview and decoded:
                preview = "[multimodal content]"
        elif isinstance(decoded, dict):
            preview = str(decoded.get("text") or decoded.get("content") or "").strip()
        else:
            preview = str(decoded or "").strip()
        preview = " ".join(preview.split())
        if len(preview) > limit:
            return preview[:limit] + "..."
        return preview

    @classmethod
    def _message_display_title_text(cls, content: Any, limit: int = 100) -> str:
        """Return a deterministic product title derived from the first user message."""
        decoded = cls._decode_content(content)
        if isinstance(decoded, list):
            parts: list[str] = []
            for item in decoded:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
                else:
                    parts.append(str(item or ""))
            title = " ".join(part for part in parts if part).strip()
            if not title and decoded:
                title = "[multimodal content]"
        elif isinstance(decoded, dict):
            title = str(decoded.get("text") or decoded.get("content") or "").strip()
        else:
            title = str(decoded or "").strip()
        title = " ".join(title.split())
        if len(title) > limit:
            return title[:limit].rstrip()
        return title

    def _rebuild_session_list_summary(self, conn: sqlite3.Connection, session_id: str) -> None:
        """Recompute list summary fields after active-message set changes."""
        row = conn.execute(
            """
            SELECT content
            FROM messages
            WHERE session_id = ?
              AND active = 1
              AND role = 'user'
              AND content IS NOT NULL
            ORDER BY timestamp, id
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        first_user_content = row["content"] if row else None
        preview = self._message_preview_text(first_user_content)
        display_title = self._message_display_title_text(first_user_content)
        conn.execute(
            """
            UPDATE sessions
            SET
                message_count = (
                    SELECT COUNT(1)
                    FROM messages m
                    WHERE m.session_id = sessions.id
                      AND m.active = 1
                ),
                preview = ?,
                display_title = CASE
                    WHEN COALESCE(display_title_source, '') = 'user' THEN COALESCE(display_title, '')
                    ELSE ?
                END,
                display_title_source = CASE
                    WHEN COALESCE(display_title_source, '') = 'user' THEN 'user'
                    WHEN ? != '' THEN 'first_user_message'
                    ELSE ''
                END,
                last_active = (
                    SELECT MAX(m.timestamp)
                    FROM messages m
                    WHERE m.session_id = sessions.id
                      AND m.active = 1
                )
            WHERE id = ?
            """,
            (preview, display_title, display_title, session_id),
        )

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str = None,
        tool_name: str = None,
        tool_calls: Any = None,
        tool_call_id: str = None,
        token_count: int = None,
        finish_reason: str = None,
        reasoning: str = None,
        reasoning_content: str = None,
        reasoning_details: Any = None,
        codex_reasoning_items: Any = None,
        codex_message_items: Any = None,
        platform_message_id: str = None,
        metadata: Any = None,
    ) -> int:
        """
        Append a message to a session. Returns the message row ID.

        Also increments the session's message_count (and tool_call_count
        if role is 'tool' or tool_calls is present).

        ``platform_message_id`` is the external messaging platform's own
        message ID (e.g. Telegram update_id, Yuanbao msg_id).  It is
        independent of the SQLite autoincrement primary key and is used by
        platform-specific flows like yuanbao's recall guard to redact a
        message by its platform-side identifier.
        """
        # Serialize structured fields to JSON before entering the write txn
        reasoning_details_json = (
            json.dumps(reasoning_details)
            if reasoning_details else None
        )
        codex_items_json = (
            json.dumps(codex_reasoning_items)
            if codex_reasoning_items else None
        )
        codex_message_items_json = (
            json.dumps(codex_message_items)
            if codex_message_items else None
        )
        tool_calls_json = json.dumps(tool_calls) if tool_calls else None
        metadata_json = json.dumps(metadata) if metadata else None
        # Multimodal content (list of parts) must be JSON-encoded: sqlite3
        # cannot bind list/dict parameters directly.
        stored_content = self._encode_content(content)

        # Pre-compute tool call count
        num_tool_calls = 0
        if tool_calls is not None:
            num_tool_calls = len(tool_calls) if isinstance(tool_calls, list) else 1
        message_timestamp = time.time()
        preview = self._message_preview_text(content) if role == "user" else ""
        display_title = self._message_display_title_text(content) if role == "user" else ""

        def _do(conn):
            cursor = conn.execute(
                """INSERT INTO messages (session_id, role, content, tool_call_id,
                   tool_calls, tool_name, timestamp, token_count, finish_reason,
                   reasoning, reasoning_content, reasoning_details, codex_reasoning_items,
                   codex_message_items, platform_message_id, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    role,
                    stored_content,
                    tool_call_id,
                    tool_calls_json,
                    tool_name,
                    message_timestamp,
                    token_count,
                    finish_reason,
                    reasoning,
                    reasoning_content,
                    reasoning_details_json,
                    codex_items_json,
                    codex_message_items_json,
                    platform_message_id,
                    metadata_json,
                ),
            )
            msg_id = cursor.lastrowid

            # Update counters
            if num_tool_calls > 0:
                conn.execute(
                    """UPDATE sessions SET message_count = message_count + 1,
                       tool_call_count = tool_call_count + ?,
                       preview = CASE
                           WHEN ? != '' AND COALESCE(preview, '') = '' THEN ?
                           ELSE COALESCE(preview, '')
                       END,
                       display_title = CASE
                           WHEN ? != '' AND COALESCE(display_title, '') = '' THEN ?
                           ELSE COALESCE(display_title, '')
                       END,
                       display_title_source = CASE
                           WHEN ? != '' AND COALESCE(display_title_source, '') = '' THEN 'first_user_message'
                           ELSE COALESCE(display_title_source, '')
                       END,
                       last_active = ?
                       WHERE id = ?""",
                    (
                        num_tool_calls,
                        preview,
                        preview,
                        display_title,
                        display_title,
                        display_title,
                        message_timestamp,
                        session_id,
                    ),
                )
            else:
                conn.execute(
                    """UPDATE sessions SET message_count = message_count + 1,
                       preview = CASE
                           WHEN ? != '' AND COALESCE(preview, '') = '' THEN ?
                           ELSE COALESCE(preview, '')
                       END,
                       display_title = CASE
                           WHEN ? != '' AND COALESCE(display_title, '') = '' THEN ?
                           ELSE COALESCE(display_title, '')
                       END,
                       display_title_source = CASE
                           WHEN ? != '' AND COALESCE(display_title_source, '') = '' THEN 'first_user_message'
                           ELSE COALESCE(display_title_source, '')
                       END,
                       last_active = ?
                       WHERE id = ?""",
                    (
                        preview,
                        preview,
                        display_title,
                        display_title,
                        display_title,
                        message_timestamp,
                        session_id,
                    ),
                )
            return msg_id

        return self._execute_write(_do)

    def replace_messages(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """Atomically replace every message for a session.

        Used by transcript-rewrite flows such as /retry, /undo, and /compress.
        The delete + reinsert sequence must commit as one transaction so a
        mid-rewrite failure does not leave SQLite with a partial transcript.
        """

        def _do(conn):
            conn.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )
            conn.execute(
                "UPDATE sessions SET message_count = 0, tool_call_count = 0, preview = '', last_active = NULL WHERE id = ?",
                (session_id,),
            )

            now_ts = time.time()
            total_messages = 0
            total_tool_calls = 0
            first_user_preview = ""
            first_user_display_title = ""
            last_message_ts = None
            for msg in messages:
                role = msg.get("role", "unknown")
                tool_calls = msg.get("tool_calls")
                message_ts = now_ts
                reasoning_details = msg.get("reasoning_details") if role == "assistant" else None
                codex_reasoning_items = (
                    msg.get("codex_reasoning_items") if role == "assistant" else None
                )
                codex_message_items = (
                    msg.get("codex_message_items") if role == "assistant" else None
                )

                reasoning_details_json = (
                    json.dumps(reasoning_details) if reasoning_details else None
                )
                codex_items_json = (
                    json.dumps(codex_reasoning_items) if codex_reasoning_items else None
                )
                codex_message_items_json = (
                    json.dumps(codex_message_items) if codex_message_items else None
                )
                tool_calls_json = json.dumps(tool_calls) if tool_calls else None
                metadata_json = json.dumps(msg.get("metadata")) if msg.get("metadata") else None
                # Accept either `platform_message_id` (new explicit name) or
                # `message_id` (yuanbao's existing convention on message dicts).
                platform_msg_id = (
                    msg.get("platform_message_id") or msg.get("message_id")
                )

                conn.execute(
                    """INSERT INTO messages (session_id, role, content, tool_call_id,
                       tool_calls, tool_name, timestamp, token_count, finish_reason,
                       reasoning, reasoning_content, reasoning_details, codex_reasoning_items,
                       codex_message_items, platform_message_id, metadata_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        role,
                        self._encode_content(msg.get("content")),
                        msg.get("tool_call_id"),
                        tool_calls_json,
                        msg.get("tool_name"),
                        message_ts,
                        msg.get("token_count"),
                        msg.get("finish_reason"),
                        msg.get("reasoning") if role == "assistant" else None,
                        msg.get("reasoning_content") if role == "assistant" else None,
                        reasoning_details_json,
                        codex_items_json,
                        codex_message_items_json,
                        platform_msg_id,
                        metadata_json,
                    ),
                )
                total_messages += 1
                if role == "user" and not first_user_preview:
                    first_user_preview = self._message_preview_text(msg.get("content"))
                    first_user_display_title = self._message_display_title_text(msg.get("content"))
                last_message_ts = message_ts
                if tool_calls is not None:
                    total_tool_calls += (
                        len(tool_calls) if isinstance(tool_calls, list) else 1
                    )
                now_ts += 1e-6

            conn.execute(
                """
                UPDATE sessions
                SET message_count = ?,
                    tool_call_count = ?,
                    preview = ?,
                    display_title = CASE
                        WHEN COALESCE(display_title_source, '') = 'user' THEN COALESCE(display_title, '')
                        ELSE ?
                    END,
                    display_title_source = CASE
                        WHEN COALESCE(display_title_source, '') = 'user' THEN 'user'
                        WHEN ? != '' THEN 'first_user_message'
                        ELSE ''
                    END,
                    last_active = ?
                WHERE id = ?
                """,
                (
                    total_messages,
                    total_tool_calls,
                    first_user_preview,
                    first_user_display_title,
                    first_user_display_title,
                    last_message_ts,
                    session_id,
                ),
            )

        self._execute_write(_do)

    def get_messages(
        self,
        session_id: str,
        include_inactive: bool = False,
    ) -> List[Dict[str, Any]]:
        """Load messages for a session, ordered by insertion order.

        Soft-deleted rewind rows are hidden by default and remain available via
        ``include_inactive=True`` for audit/debug views.
        """
        active_clause = "" if include_inactive else " AND active = 1"
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ?"
                f"{active_clause} ORDER BY id",
                (session_id,),
            )
            rows = cursor.fetchall()
        result = []
        for row in rows:
            msg = dict(row)
            if "content" in msg:
                msg["content"] = self._decode_content(msg["content"])
            if msg.get("tool_calls"):
                try:
                    msg["tool_calls"] = json.loads(msg["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Failed to deserialize tool_calls in get_messages, falling back to []")
                    msg["tool_calls"] = []
            if msg.get("metadata_json"):
                try:
                    msg["metadata"] = json.loads(msg["metadata_json"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Failed to deserialize metadata_json in get_messages, falling back to None")
                    msg["metadata"] = None
            result.append(msg)
        return result

    @staticmethod
    def _merge_message_metadata(current: Any, patch: Dict[str, Any]) -> Dict[str, Any]:
        base = dict(current) if isinstance(current, dict) else {}
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                base[key] = SessionDB._merge_message_metadata(base[key], value)
            else:
                base[key] = value
        return base

    @staticmethod
    def _metadata_matches_turn(metadata: Any, *, run_id: str, turn_id: str, client_message_id: str) -> bool:
        if not isinstance(metadata, dict):
            return False
        return any(
            expected and str(metadata.get(key) or "").strip() == expected
            for key, expected in (
                ("run_id", run_id),
                ("turn_id", turn_id),
                ("client_message_id", client_message_id),
            )
        )

    def merge_message_metadata(
        self,
        session_id: str,
        metadata: Dict[str, Any],
        *,
        message_id: str | int | None = None,
        role: str | None = None,
        run_id: str | None = None,
        turn_id: str | None = None,
        client_message_id: str | None = None,
    ) -> Optional[Dict[str, Any]]:
        """Merge metadata into a stored message and return the updated message."""
        if not isinstance(metadata, dict) or not metadata:
            return None
        target_role = str(role or "").strip()
        target_run_id = str(run_id or "").strip()
        target_turn_id = str(turn_id or "").strip()
        target_client_message_id = str(client_message_id or "").strip()
        target_message_id = str(message_id or "").strip()

        def _row_metadata(row) -> Dict[str, Any]:
            raw = row["metadata_json"]
            if not raw:
                return {}
            try:
                value = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                logger.warning("Failed to deserialize message metadata for merge")
                return {}
            return value if isinstance(value, dict) else {}

        def _select_target_row(conn):
            if target_message_id:
                try:
                    numeric_message_id = int(target_message_id)
                except (TypeError, ValueError):
                    numeric_message_id = None
                if numeric_message_id is not None:
                    row = conn.execute(
                        "SELECT * FROM messages WHERE id = ? AND session_id = ?",
                        (numeric_message_id, session_id),
                    ).fetchone()
                    if row is not None:
                        return row

            if not (target_run_id or target_turn_id or target_client_message_id):
                return None

            active_clause = "AND role = ?" if target_role else ""
            params: list[Any] = [session_id]
            if target_role:
                params.append(target_role)
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? "
                f"{active_clause} "
                "AND metadata_json IS NOT NULL ORDER BY id DESC",
                tuple(params),
            ).fetchall()
            for row in rows:
                if self._metadata_matches_turn(
                    _row_metadata(row),
                    run_id=target_run_id,
                    turn_id=target_turn_id,
                    client_message_id=target_client_message_id,
                ):
                    return row
            return None

        def _do(conn):
            row = _select_target_row(conn)
            if row is None:
                return None
            next_metadata = self._merge_message_metadata(_row_metadata(row), metadata)
            conn.execute(
                "UPDATE messages SET metadata_json = ? WHERE id = ?",
                (
                    json.dumps(next_metadata, ensure_ascii=False),
                    row["id"],
                ),
            )
            updated = dict(row)
            updated["metadata_json"] = json.dumps(next_metadata, ensure_ascii=False)
            return self._message_row_as_conversation(
                updated,
                include_storage_metadata=True,
            )

        return self._execute_write(_do)

    def get_messages_around(
        self,
        session_id: str,
        around_message_id: int,
        window: int = 5,
        include_inactive: bool = False,
    ) -> Dict[str, Any]:
        """Load a window of messages anchored on a specific message id.

        Returns a dict with:
          - ``window``: up to ``window`` messages before the anchor, the anchor
            itself, and up to ``window`` messages after, ordered by id ascending.
          - ``messages_before``: count of messages strictly before the anchor
            still in the session (== window unless we hit the start).
          - ``messages_after``: count of messages strictly after the anchor
            still in the session (== window unless we hit the end).

        Used by ``session_search`` for both the discovery shape (anchored on the
        FTS5 match) and the scroll shape (anchored on any message id). The
        ``messages_before`` / ``messages_after`` counts let the caller detect
        session boundaries: when either is less than ``window``, the agent has
        reached one end of the session.

        Returns an empty window when ``around_message_id`` is not a real id in
        ``session_id`` — callers decide how to surface that.
        """
        if window < 0:
            window = 0
        active_clause = "" if include_inactive else " AND active = 1"
        with self._lock:
            # Confirm the anchor exists in this session.
            anchor_exists = self._conn.execute(
                "SELECT 1 FROM messages WHERE id = ? AND session_id = ?"
                f"{active_clause} LIMIT 1",
                (around_message_id, session_id),
            ).fetchone()
            if not anchor_exists:
                return {"window": [], "messages_before": 0, "messages_after": 0}

            # Two queries: anchor + before (DESC, take window+1), and after
            # (ASC, take window). Final order is id ASC.
            before_rows = self._conn.execute(
                "SELECT * FROM messages "
                "WHERE session_id = ? AND id <= ? "
                f"{active_clause} "
                "ORDER BY id DESC LIMIT ?",
                (session_id, around_message_id, window + 1),
            ).fetchall()
            after_rows = self._conn.execute(
                "SELECT * FROM messages "
                "WHERE session_id = ? AND id > ? "
                f"{active_clause} "
                "ORDER BY id ASC LIMIT ?",
                (session_id, around_message_id, window),
            ).fetchall()

        # before_rows is DESC; reverse so it's ASC, then concatenate after_rows.
        rows = list(reversed(before_rows)) + list(after_rows)
        result = []
        for row in rows:
            msg = dict(row)
            if "content" in msg:
                msg["content"] = self._decode_content(msg["content"])
            if msg.get("tool_calls"):
                try:
                    msg["tool_calls"] = json.loads(msg["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning(
                        "Failed to deserialize tool_calls in get_messages_around, falling back to []"
                    )
                    msg["tool_calls"] = []
            result.append(msg)

        # before_rows includes the anchor itself; subtract 1 for the count of
        # messages strictly before the anchor in the returned slice.
        messages_before = max(0, len(before_rows) - 1)
        messages_after = len(after_rows)
        return {
            "window": result,
            "messages_before": messages_before,
            "messages_after": messages_after,
        }

    def get_anchored_view(
        self,
        session_id: str,
        around_message_id: int,
        window: int = 5,
        bookend: int = 3,
        keep_roles: Optional[Tuple[str, ...]] = ("user", "assistant"),
        include_inactive: bool = False,
    ) -> Dict[str, Any]:
        """Return an anchored window plus session bookends.

        Built on top of ``get_messages_around``. Three slices:

          - ``window``: messages immediately surrounding the anchor. Filtered
            to ``keep_roles`` (tool-response noise dropped by default), EXCEPT
            the anchor itself is always preserved regardless of role.
          - ``bookend_start``: first ``bookend`` user/assistant messages of the
            session — but only those whose id is strictly before the window's
            first message id. Empty when the window already overlaps the
            session head. Empty-content messages (tool-call-only assistant
            turns) are skipped so they don't crowd out actual prose openings.
          - ``bookend_end``: last ``bookend`` user/assistant messages of the
            session, same non-overlap rule at the tail.

        Bookends let an FTS5 hit anywhere in a long session yield the goal
        (opening) and the resolution (closing) on a single call — without
        loading the whole transcript.

        Returns ``{"window": [], "messages_before": 0, "messages_after": 0,
        "bookend_start": [], "bookend_end": []}`` when the anchor isn't in
        the session.

        ``keep_roles=None`` disables role filtering (raw window + raw
        bookends).
        """
        if bookend < 0:
            bookend = 0

        # Reuse the primitive — handles anchor-existence, content decoding,
        # tool_calls deserialisation, and boundary counts.
        primitive = self.get_messages_around(
            session_id,
            around_message_id,
            window=window,
            include_inactive=include_inactive,
        )
        window_rows = primitive["window"]
        if not window_rows:
            return {
                "window": [],
                "messages_before": 0,
                "messages_after": 0,
                "bookend_start": [],
                "bookend_end": [],
            }

        # Apply role filter to the window, but never drop the anchor itself.
        if keep_roles is not None:
            keep_set = set(keep_roles)
            filtered_window = [
                m for m in window_rows
                if m.get("id") == around_message_id or m.get("role") in keep_set
            ]
        else:
            filtered_window = window_rows

        window_min_id = window_rows[0]["id"]
        window_max_id = window_rows[-1]["id"]

        # Fetch bookends only when there's room outside the window. SQL filters
        # by id range, role, and non-empty content — tool-call-only assistant
        # turns (content='' with tool_calls populated) are excluded so they
        # don't crowd out actual prose openings/closings.
        bookend_start_rows: List[Any] = []
        bookend_end_rows: List[Any] = []
        if bookend > 0:
            with self._lock:
                active_clause = "" if include_inactive else " AND active = 1"
                role_clause = ""
                role_params: list = []
                if keep_roles is not None:
                    role_placeholders = ",".join("?" for _ in keep_roles)
                    role_clause = f" AND role IN ({role_placeholders})"
                    role_params = list(keep_roles)

                bookend_start_rows = self._conn.execute(
                    f"SELECT * FROM messages "
                    f"WHERE session_id = ? AND id < ?{active_clause}{role_clause} "
                    f"AND length(content) > 0 "
                    f"ORDER BY id ASC LIMIT ?",
                    (session_id, window_min_id, *role_params, bookend),
                ).fetchall()

                bookend_end_rows = self._conn.execute(
                    f"SELECT * FROM messages "
                    f"WHERE session_id = ? AND id > ?{active_clause}{role_clause} "
                    f"AND length(content) > 0 "
                    f"ORDER BY id DESC LIMIT ?",
                    (session_id, window_max_id, *role_params, bookend),
                ).fetchall()
                # End rows came back DESC for the LIMIT cap; flip to ASC.
                bookend_end_rows = list(reversed(bookend_end_rows))

        def _hydrate(row) -> Dict[str, Any]:
            msg = dict(row)
            if "content" in msg:
                msg["content"] = self._decode_content(msg["content"])
            if msg.get("tool_calls"):
                try:
                    msg["tool_calls"] = json.loads(msg["tool_calls"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning(
                        "Failed to deserialize tool_calls in get_anchored_view, falling back to []"
                    )
                    msg["tool_calls"] = []
            return msg

        return {
            "window": filtered_window,
            "messages_before": primitive["messages_before"],
            "messages_after": primitive["messages_after"],
            "bookend_start": [_hydrate(r) for r in bookend_start_rows],
            "bookend_end": [_hydrate(r) for r in bookend_end_rows],
        }

    def resolve_resume_session_id(self, session_id: str) -> str:
        """Redirect a resume target to the descendant session that holds the messages.

        Context compression ends the current session and forks a new child session
        (linked via ``parent_session_id``). The flush cursor is reset, so the
        child is where new messages actually land — the parent ends up with
        ``message_count = 0`` rows unless messages had already been flushed to
        it before compression. See #15000.

        This helper walks ``parent_session_id`` forward from ``session_id`` and
        returns the first descendant in the chain that has at least one message
        row. If the original session already has messages, or no descendant
        has any, the original ``session_id`` is returned unchanged.

        The chain is always walked via the child whose ``started_at`` is
        latest; that matches the single-chain shape that compression creates.
        A depth cap (32) guards against accidental loops in malformed data.
        """
        if not session_id:
            return session_id

        with self._lock:
            # If this session already has messages, nothing to redirect.
            try:
                row = self._conn.execute(
                    "SELECT 1 FROM messages WHERE session_id = ? AND active = 1 LIMIT 1",
                    (session_id,),
                ).fetchone()
            except Exception:
                return session_id
            if row is not None:
                return session_id

            # Walk descendants: at each step, pick the most-recently-started
                # child session; stop once we find one with messages.
            current = session_id
            seen = {current}
            for _ in range(32):
                try:
                    child_row = self._conn.execute(
                        "SELECT id FROM sessions "
                        "WHERE parent_session_id = ? "
                        "ORDER BY started_at DESC, id DESC LIMIT 1",
                        (current,),
                    ).fetchone()
                except Exception:
                    return session_id
                if child_row is None:
                    return session_id
                child_id = child_row["id"] if hasattr(child_row, "keys") else child_row[0]
                if not child_id or child_id in seen:
                    return session_id
                seen.add(child_id)
                try:
                    msg_row = self._conn.execute(
                        "SELECT 1 FROM messages WHERE session_id = ? AND active = 1 LIMIT 1",
                        (child_id,),
                    ).fetchone()
                except Exception:
                    return session_id
                if msg_row is not None:
                    return child_id
                current = child_id
        return session_id

    def _message_row_as_conversation(
        self,
        row,
        *,
        include_storage_metadata: bool = False,
    ) -> Dict[str, Any]:
        content = self._decode_content(row["content"])
        if row["role"] in {"user", "assistant"} and isinstance(content, str):
            content = sanitize_context(content).strip()
        msg = {"role": row["role"], "content": content}
        if include_storage_metadata:
            msg["message_id"] = str(row["id"])
            msg["timestamp"] = row["timestamp"]
        elif row["platform_message_id"]:
            # Surface the platform-side message id (e.g. yuanbao msg_id,
            # telegram update_id) so platform-specific flows like recall
            # can match by external identifier instead of having to fall
            # back to content-match heuristics.  Exposed as ``message_id``
            # for backward compatibility with the JSONL transcript shape.
            msg["message_id"] = row["platform_message_id"]
        if row["tool_call_id"]:
            msg["tool_call_id"] = row["tool_call_id"]
        if row["tool_name"]:
            msg["tool_name"] = row["tool_name"]
        if row["tool_calls"]:
            try:
                msg["tool_calls"] = json.loads(row["tool_calls"])
            except (json.JSONDecodeError, TypeError):
                logger.warning("Failed to deserialize tool_calls in conversation replay, falling back to []")
                msg["tool_calls"] = []
        if row["role"] == "assistant":
            if row["finish_reason"]:
                msg["finish_reason"] = row["finish_reason"]
            if row["reasoning"]:
                msg["reasoning"] = row["reasoning"]
            if row["reasoning_content"] is not None:
                msg["reasoning_content"] = row["reasoning_content"]
            if row["reasoning_details"]:
                try:
                    msg["reasoning_details"] = json.loads(row["reasoning_details"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Failed to deserialize reasoning_details, falling back to None")
                    msg["reasoning_details"] = None
            if row["codex_reasoning_items"]:
                try:
                    msg["codex_reasoning_items"] = json.loads(row["codex_reasoning_items"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Failed to deserialize codex_reasoning_items, falling back to None")
                    msg["codex_reasoning_items"] = None
            if row["codex_message_items"]:
                try:
                    msg["codex_message_items"] = json.loads(row["codex_message_items"])
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Failed to deserialize codex_message_items, falling back to None")
                    msg["codex_message_items"] = None
        if row["metadata_json"]:
            try:
                msg["metadata"] = json.loads(row["metadata_json"])
            except (json.JSONDecodeError, TypeError):
                logger.warning("Failed to deserialize message metadata, falling back to None")
                msg["metadata"] = None
        return msg

    def _conversation_message_columns(self) -> str:
        return (
            "id, session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp, "
            "finish_reason, reasoning, reasoning_content, reasoning_details, "
            "codex_reasoning_items, codex_message_items, platform_message_id, metadata_json"
        )

    @staticmethod
    def _message_row_turn_metadata(row) -> Dict[str, str]:
        try:
            raw_metadata = row["metadata_json"]
        except (KeyError, IndexError):
            return {}
        if not raw_metadata:
            return {}
        try:
            metadata = json.loads(raw_metadata)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Failed to deserialize message metadata for turn expansion")
            return {}
        if not isinstance(metadata, dict):
            return {}

        identity: Dict[str, str] = {}
        for key in ("turn_id", "run_id", "client_message_id"):
            value = metadata.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                identity[key] = text
        return identity

    def _expand_message_page_rows_to_turn_boundaries(
        self,
        rows: List[Any],
        *,
        session_ids: List[str],
        columns: str,
        include_inactive: bool = False,
    ) -> List[Any]:
        if not rows:
            return rows

        selected_identity_values: Dict[str, set[str]] = {
            "turn_id": set(),
            "run_id": set(),
            "client_message_id": set(),
        }
        for row in rows:
            identity = self._message_row_turn_metadata(row)
            for key, value in identity.items():
                selected_identity_values[key].add(value)

        if not any(selected_identity_values.values()):
            return rows

        placeholders = ",".join("?" for _ in session_ids)
        active_clause = "" if include_inactive else " AND active = 1"
        candidate_rows = self._conn.execute(
            f"SELECT {columns} FROM messages "
            f"WHERE session_id IN ({placeholders}) AND metadata_json IS NOT NULL "
            f"{active_clause} "
            "ORDER BY id",
            tuple(session_ids),
        ).fetchall()

        rows_by_id = {int(row["id"]): row for row in rows}
        matched_min_id_by_session: Dict[str, int] = {}
        matched_user_sessions: set[str] = set()

        for row in candidate_rows:
            identity = self._message_row_turn_metadata(row)
            if not any(
                value in selected_identity_values[key]
                for key, value in identity.items()
                if key in selected_identity_values
            ):
                continue

            row_id = int(row["id"])
            rows_by_id[row_id] = row
            row_session_id = str(row["session_id"])
            current_min = matched_min_id_by_session.get(row_session_id)
            if current_min is None or row_id < current_min:
                matched_min_id_by_session[row_session_id] = row_id
            if row["role"] == "user":
                matched_user_sessions.add(row_session_id)

        for row_session_id, first_matched_id in matched_min_id_by_session.items():
            if row_session_id in matched_user_sessions:
                continue
            previous_user = self._conn.execute(
                f"SELECT {columns} FROM messages "
                "WHERE session_id = ? AND role = 'user' AND id < ? "
                f"{active_clause} "
                "ORDER BY id DESC LIMIT 1",
                (row_session_id, first_matched_id),
            ).fetchone()
            if previous_user is not None:
                rows_by_id[int(previous_user["id"])] = previous_user

        return [rows_by_id[row_id] for row_id in sorted(rows_by_id)]

    def _has_messages_on_page_side(
        self,
        session_ids: List[str],
        *,
        row_id: Optional[int],
        side: str,
        include_inactive: bool = False,
    ) -> bool:
        if row_id is None:
            return False
        placeholders = ",".join("?" for _ in session_ids)
        operator = "<" if side == "before" else ">"
        active_clause = "" if include_inactive else " AND active = 1"
        row = self._conn.execute(
            f"SELECT 1 FROM messages "
            f"WHERE session_id IN ({placeholders}) AND id {operator} ? "
            f"{active_clause} "
            "LIMIT 1",
            tuple(session_ids) + (row_id,),
        ).fetchone()
        return row is not None

    def get_messages_as_conversation(
        self,
        session_id: str,
        include_ancestors: bool = False,
        include_storage_metadata: bool = False,
        include_inactive: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Load messages in the OpenAI conversation format (role + content dicts).
        Used by the gateway to restore conversation history.
        """
        session_ids = [session_id]
        if include_ancestors:
            session_ids = self._session_lineage_root_to_tip(session_id)

        active_clause = "" if include_inactive else " AND active = 1"
        with self._lock:
            placeholders = ",".join("?" for _ in session_ids)
            rows = self._conn.execute(
                f"SELECT {self._conversation_message_columns()} "
                f"FROM messages WHERE session_id IN ({placeholders})"
                f"{active_clause} ORDER BY id",
                tuple(session_ids),
            ).fetchall()

        messages = []
        for row in rows:
            msg = self._message_row_as_conversation(
                row,
                include_storage_metadata=include_storage_metadata,
            )
            if include_ancestors and self._is_duplicate_replayed_user_message(messages, msg):
                continue
            messages.append(msg)
        return messages

    def get_messages_page_as_conversation(
        self,
        session_id: str,
        direction: str = "tail",
        cursor_id: Optional[int] = None,
        limit: int = 50,
        include_ancestors: bool = False,
        include_inactive: bool = False,
    ) -> Dict[str, Any]:
        """Load one stable page of conversation messages with storage cursors.

        ``direction`` accepts:
          - ``tail``: newest ``limit`` messages, returned oldest-to-newest.
          - ``before``: ``limit`` messages older than ``cursor_id``.
          - ``after``: ``limit`` messages newer than ``cursor_id``.

        Cursors are SQLite message row ids.  The returned messages include a
        string ``message_id`` based on that row id so clients can dedupe pages
        without relying on mutable text content.
        """
        try:
            page_limit = int(limit)
        except (TypeError, ValueError):
            page_limit = 50
        page_limit = max(1, min(page_limit, 500))

        session_ids = [session_id]
        if include_ancestors:
            session_ids = self._session_lineage_root_to_tip(session_id)

        normalized_direction = str(direction or "tail").lower()
        if normalized_direction not in {"tail", "before", "after"}:
            normalized_direction = "tail"

        with self._lock:
            placeholders = ",".join("?" for _ in session_ids)
            base_params: Tuple[Any, ...] = tuple(session_ids)
            active_clause = "" if include_inactive else " AND active = 1"
            total_count = self._conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE session_id IN ({placeholders})"
                f"{active_clause}",
                base_params,
            ).fetchone()[0]

            columns = self._conversation_message_columns()
            if normalized_direction == "before" and cursor_id is not None:
                rows = self._conn.execute(
                    f"SELECT {columns} FROM messages "
                    f"WHERE session_id IN ({placeholders}) AND id < ? "
                    f"{active_clause} "
                    "ORDER BY id DESC LIMIT ?",
                    base_params + (cursor_id, page_limit + 1),
                ).fetchall()
                has_more_before = len(rows) > page_limit
                selected_rows = list(reversed(rows[:page_limit]))
                has_more_after = bool(selected_rows)
            elif normalized_direction == "after" and cursor_id is not None:
                rows = self._conn.execute(
                    f"SELECT {columns} FROM messages "
                    f"WHERE session_id IN ({placeholders}) AND id > ? "
                    f"{active_clause} "
                    "ORDER BY id ASC LIMIT ?",
                    base_params + (cursor_id, page_limit + 1),
                ).fetchall()
                has_more_after = len(rows) > page_limit
                selected_rows = list(rows[:page_limit])
                has_more_before = bool(selected_rows)
            else:
                rows = self._conn.execute(
                    f"SELECT {columns} FROM messages "
                    f"WHERE session_id IN ({placeholders}) "
                    f"{active_clause} "
                    "ORDER BY id DESC LIMIT ?",
                    base_params + (page_limit + 1,),
                ).fetchall()
                has_more_before = len(rows) > page_limit
                selected_rows = list(reversed(rows[:page_limit]))
                has_more_after = False

            selected_rows = self._expand_message_page_rows_to_turn_boundaries(
                selected_rows,
                session_ids=session_ids,
                columns=columns,
                include_inactive=include_inactive,
            )
            first_id = int(selected_rows[0]["id"]) if selected_rows else None
            last_id = int(selected_rows[-1]["id"]) if selected_rows else None
            has_more_before = self._has_messages_on_page_side(
                session_ids,
                row_id=first_id,
                side="before",
                include_inactive=include_inactive,
            )
            has_more_after = self._has_messages_on_page_side(
                session_ids,
                row_id=last_id,
                side="after",
                include_inactive=include_inactive,
            )

        messages = []
        for row in selected_rows:
            msg = self._message_row_as_conversation(
                row,
                include_storage_metadata=True,
            )
            if include_ancestors and self._is_duplicate_replayed_user_message(messages, msg):
                continue
            messages.append(msg)

        return {
            "messages": messages,
            "pageInfo": {
                "prev_cursor_id": first_id if has_more_before else None,
                "next_cursor_id": last_id if has_more_after else None,
                "hasMoreBefore": has_more_before,
                "hasMoreAfter": has_more_after,
                "totalCount": int(total_count or 0),
            },
        }

    def _session_lineage_root_to_tip(self, session_id: str) -> List[str]:
        if not session_id:
            return [session_id]

        with self._lock:
            return self._replayable_lineage_root_to_tip_conn(self._conn, session_id)

    @staticmethod
    def _is_duplicate_replayed_user_message(messages: List[Dict[str, Any]], msg: Dict[str, Any]) -> bool:
        if msg.get("role") != "user":
            return False
        content = msg.get("content")
        if not isinstance(content, str) or not content:
            return False
        for prev in reversed(messages):
            if prev.get("role") == "user" and prev.get("content") == content:
                return True
            if prev.get("role") == "assistant" and (prev.get("content") or prev.get("tool_calls")):
                return False
        return False

    # =========================================================================
    # Rewind (soft-delete)
    # =========================================================================

    def rewind_to_message(
        self,
        session_id: str,
        target_message_id: int,
    ) -> Dict[str, Any]:
        """Soft-delete the target user message and every following row.

        The rows remain on disk with ``active=0`` for audit/debug views. Normal
        transcript reads, search, resume and pagination ignore inactive rows by
        default. The target row is included in the soft-delete so callers can
        prefill it into the composer without duplicating it in the replayed
        context.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE id = ? AND session_id = ?",
                (target_message_id, session_id),
            ).fetchone()
        if row is None:
            raise ValueError(
                f"message {target_message_id} not found in session {session_id}"
            )

        target_row = dict(row)
        if target_row.get("role") != "user":
            raise ValueError(
                "rewind target must be a 'user' message "
                f"(got role={target_row.get('role')!r}, id={target_message_id})"
            )
        target_row["content"] = self._decode_content(target_row.get("content"))

        def _do(conn):
            cursor = conn.execute(
                "SELECT id FROM messages "
                "WHERE session_id = ? AND id >= ? AND active = 1",
                (session_id, target_message_id),
            )
            ids = [int(r[0]) for r in cursor.fetchall()]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE messages SET active = 0 WHERE id IN ({placeholders})",
                    ids,
                )
            conn.execute(
                "UPDATE sessions "
                "SET rewind_count = COALESCE(rewind_count, 0) + 1 "
                "WHERE id = ?",
                (session_id,),
            )
            self._rebuild_session_list_summary(conn, session_id)
            return ids

        rewound_ids = self._execute_write(_do)

        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(id) FROM messages WHERE session_id = ? AND active = 1",
                (session_id,),
            ).fetchone()
        new_head_id = row[0] if row and row[0] is not None else None

        return {
            "rewound_count": len(rewound_ids),
            "target_message": target_row,
            "new_head_id": new_head_id,
        }

    def restore_rewound(self, session_id: str, since_message_id: int) -> int:
        """Restore inactive rows from ``since_message_id`` onward."""

        def _do(conn):
            cursor = conn.execute(
                "SELECT id FROM messages "
                "WHERE session_id = ? AND id >= ? AND active = 0",
                (session_id, since_message_id),
            )
            ids = [int(r[0]) for r in cursor.fetchall()]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE messages SET active = 1 WHERE id IN ({placeholders})",
                    ids,
                )
            self._rebuild_session_list_summary(conn, session_id)
            return len(ids)

        return self._execute_write(_do)

    def list_recent_user_messages(
        self,
        session_id: str,
        limit: int = 20,
        include_inactive: bool = False,
    ) -> List[Dict[str, Any]]:
        """Return recent user messages newest-first for undo/rewind selection."""
        try:
            bounded_limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            bounded_limit = 20
        active_clause = "" if include_inactive else " AND active = 1"
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, timestamp, content FROM messages "
                "WHERE session_id = ? AND role = 'user'"
                f"{active_clause} "
                "ORDER BY id DESC LIMIT ?",
                (session_id, bounded_limit),
            ).fetchall()

        result: List[Dict[str, Any]] = []
        for row in rows:
            decoded = self._decode_content(row["content"])
            if isinstance(decoded, list):
                text_parts = [
                    part.get("text", "")
                    for part in decoded
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                preview = " ".join(part for part in text_parts if part).strip()
                if not preview:
                    preview = "[multimodal content]"
            elif isinstance(decoded, str):
                preview = decoded
            else:
                preview = ""
            preview = " ".join(preview.split())
            if len(preview) > 80:
                preview = preview[:77] + "..."
            result.append(
                {
                    "id": row["id"],
                    "timestamp": row["timestamp"],
                    "preview": preview,
                }
            )
        return result

    # =========================================================================
    # Search
    # =========================================================================

    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        """Sanitize user input for safe use in FTS5 MATCH queries.

        FTS5 has its own query syntax where characters like ``"``, ``(``, ``)``,
        ``+``, ``*``, ``{``, ``}`` and bare boolean operators (``AND``, ``OR``,
        ``NOT``) have special meaning.  Passing raw user input directly to
        MATCH can cause ``sqlite3.OperationalError``.

        Strategy:
        - Preserve properly paired quoted phrases (``"exact phrase"``)
        - Strip unmatched FTS5-special characters that would cause errors
        - Wrap unquoted hyphenated and dotted terms in quotes so FTS5
          matches them as exact phrases instead of splitting on the
          hyphen/dot (e.g. ``chat-send``, ``P2.2``, ``my-app.config.ts``)
        """
        # Step 1: Extract balanced double-quoted phrases and protect them
        # from further processing via numbered placeholders.
        _quoted_parts: list = []

        def _preserve_quoted(m: re.Match) -> str:
            _quoted_parts.append(m.group(0))
            return f"\x00Q{len(_quoted_parts) - 1}\x00"

        sanitized = re.sub(r'"[^"]*"', _preserve_quoted, query)

        # Step 2: Strip remaining (unmatched) FTS5-special characters
        sanitized = re.sub(r'[+{}()\"^]', " ", sanitized)

        # Step 3: Collapse repeated * (e.g. "***") into a single one,
        # and remove leading * (prefix-only needs at least one char before *)
        sanitized = re.sub(r"\*+", "*", sanitized)
        sanitized = re.sub(r"(^|\s)\*", r"\1", sanitized)

        # Step 4: Remove dangling boolean operators at start/end that would
        # cause syntax errors (e.g. "hello AND" or "OR world")
        sanitized = re.sub(r"(?i)^(AND|OR|NOT)\b\s*", "", sanitized.strip())
        sanitized = re.sub(r"(?i)\s+(AND|OR|NOT)\s*$", "", sanitized.strip())

        # Step 5: Wrap unquoted dotted and/or hyphenated terms in double
        # quotes.  FTS5's tokenizer splits on dots and hyphens, turning
        # ``chat-send`` into ``chat AND send`` and ``P2.2`` into ``p2 AND 2``.
        # Quoting preserves phrase semantics.  A single pass avoids the
        # double-quoting bug that would occur if dotted, hyphenated and underscored
        # patterns were applied sequentially (e.g. ``my-app.config``).
        sanitized = re.sub(r"\b(\w+(?:[._-]\w+)+)\b", r'"\1"', sanitized)

        # Step 6: Restore preserved quoted phrases
        for i, quoted in enumerate(_quoted_parts):
            sanitized = sanitized.replace(f"\x00Q{i}\x00", quoted)

        return sanitized.strip()


    @staticmethod
    def _is_cjk_codepoint(cp: int) -> bool:
        return (0x4E00 <= cp <= 0x9FFF or    # CJK Unified Ideographs
                0x3400 <= cp <= 0x4DBF or    # CJK Extension A
                0x20000 <= cp <= 0x2A6DF or  # CJK Extension B
                0x3000 <= cp <= 0x303F or    # CJK Symbols
                0x3040 <= cp <= 0x309F or    # Hiragana
                0x30A0 <= cp <= 0x30FF or    # Katakana
                0xAC00 <= cp <= 0xD7AF)      # Hangul Syllables

    @staticmethod
    def _contains_cjk(text: str) -> bool:
        """Check if text contains CJK (Chinese, Japanese, Korean) characters."""
        for ch in text:
            cp = ord(ch)
            if (0x4E00 <= cp <= 0x9FFF or    # CJK Unified Ideographs
                0x3400 <= cp <= 0x4DBF or    # CJK Extension A
                0x20000 <= cp <= 0x2A6DF or  # CJK Extension B
                0x3000 <= cp <= 0x303F or    # CJK Symbols
                0x3040 <= cp <= 0x309F or    # Hiragana
                0x30A0 <= cp <= 0x30FF or    # Katakana
                0xAC00 <= cp <= 0xD7AF):     # Hangul Syllables
                return True
        return False

    @classmethod
    def _count_cjk(cls, text: str) -> int:
        """Count CJK characters in text."""
        return sum(1 for ch in text if cls._is_cjk_codepoint(ord(ch)))

    def search_messages(
        self,
        query: str,
        source_filter: List[str] = None,
        exclude_sources: List[str] = None,
        role_filter: List[str] = None,
        limit: int = 20,
        offset: int = 0,
        sort: str = None,
        include_inactive: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Full-text search across session messages using FTS5.

        Supports FTS5 query syntax:
          - Simple keywords: "docker deployment"
          - Phrases: '"exact phrase"'
          - Boolean: "docker OR kubernetes", "python NOT java"
          - Prefix: "deploy*"

        Returns matching messages with session metadata, content snippet,
        and surrounding context (1 message before and after the match).

        ``sort`` controls temporal ordering:
          - ``None`` (default): FTS5 BM25 relevance only. Time-neutral.
          - ``"newest"``: order by message timestamp DESC, then by rank.
          - ``"oldest"``: order by message timestamp ASC, then by rank.

        The short-CJK LIKE fallback already orders by timestamp DESC and
        ignores ``sort``. The trigram CJK path honours ``sort`` like the main
        FTS5 path.
        """
        if not query or not query.strip():
            return []

        query = self._sanitize_fts5_query(query)
        if not query:
            return []

        # Normalise sort. Anything not in the allowed set falls back to None
        # (FTS5 rank-only) so callers can pass through user input without
        # validation.
        if isinstance(sort, str):
            sort_norm = sort.strip().lower()
            if sort_norm not in ("newest", "oldest"):
                sort_norm = None
        else:
            sort_norm = None

        # ORDER BY shared across the main FTS5 path and trigram CJK path.
        # With sort set, timestamp is primary and rank is the tiebreaker.
        if sort_norm == "newest":
            order_by_sql = "ORDER BY m.timestamp DESC, rank"
        elif sort_norm == "oldest":
            order_by_sql = "ORDER BY m.timestamp ASC, rank"
        else:
            order_by_sql = "ORDER BY rank"

        # Build WHERE clauses dynamically
        where_clauses = ["messages_fts MATCH ?"]
        params: list = [query]
        if not include_inactive:
            where_clauses.append("m.active = 1")

        if source_filter is not None:
            source_placeholders = ",".join("?" for _ in source_filter)
            where_clauses.append(f"s.source IN ({source_placeholders})")
            params.extend(source_filter)

        if exclude_sources is not None:
            exclude_placeholders = ",".join("?" for _ in exclude_sources)
            where_clauses.append(f"s.source NOT IN ({exclude_placeholders})")
            params.extend(exclude_sources)

        if role_filter:
            role_placeholders = ",".join("?" for _ in role_filter)
            where_clauses.append(f"m.role IN ({role_placeholders})")
            params.extend(role_filter)

        where_sql = " AND ".join(where_clauses)
        params.extend([limit, offset])

        sql = f"""
            SELECT
                m.id,
                m.session_id,
                m.role,
                snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet,
                m.content,
                m.timestamp,
                m.tool_name,
                s.source,
                s.model,
                s.started_at AS session_started
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.id = m.session_id
            WHERE {where_sql}
            {order_by_sql}
            LIMIT ? OFFSET ?
        """

        # CJK queries bypass the unicode61 FTS5 table.  The default tokenizer
        # splits CJK characters into individual tokens, so "大别山项目" becomes
        # "大 AND 别 AND 山 AND 项 AND 目" — producing false positives and
        # missing exact phrase matches.
        #
        # For queries with 3+ CJK characters, we use the trigram FTS5 table
        # (indexed substring matching with ranking and snippets).  For shorter
        # CJK queries (1-2 chars), trigram can't match (it needs ≥9 UTF-8
        # bytes = 3 CJK chars), so we fall back to LIKE.
        is_cjk = self._contains_cjk(query)
        if is_cjk:
            raw_query = query.strip('"').strip()
            cjk_count = self._count_cjk(raw_query)

            # Per-token CJK length check (#20494): trigram needs >=3 CJK chars
            # per token. A query like "广西 OR 桂林 OR 漓江" has cjk_count=6
            # (>=3) but each individual token is only 2 chars — trigram returns 0.
            # Route to LIKE when any non-operator CJK token is <3 CJK chars.
            _tokens_for_check = [
                t for t in raw_query.split()
                if t.upper() not in {"AND", "OR", "NOT"} and self._contains_cjk(t)
            ]
            _any_short_cjk = any(
                self._count_cjk(t) < 3 for t in _tokens_for_check
            )

            if cjk_count >= 3 and not _any_short_cjk:
                # Trigram FTS5 path — quote each non-operator token to handle
                # FTS5 special chars (%, *, etc.) while preserving boolean
                # operators (AND, OR, NOT) for multi-term queries.
                tokens = raw_query.split()
                parts = []
                for tok in tokens:
                    if tok.upper() in {"AND", "OR", "NOT"}:
                        parts.append(tok)
                    else:
                        parts.append('"' + tok.replace('"', '""') + '"')
                trigram_query = " ".join(parts)
                tri_where = ["messages_fts_trigram MATCH ?"]
                tri_params: list = [trigram_query]
                if not include_inactive:
                    tri_where.append("m.active = 1")
                if source_filter is not None:
                    tri_where.append(f"s.source IN ({','.join('?' for _ in source_filter)})")
                    tri_params.extend(source_filter)
                if exclude_sources is not None:
                    tri_where.append(f"s.source NOT IN ({','.join('?' for _ in exclude_sources)})")
                    tri_params.extend(exclude_sources)
                if role_filter:
                    tri_where.append(f"m.role IN ({','.join('?' for _ in role_filter)})")
                    tri_params.extend(role_filter)
                tri_sql = f"""
                    SELECT
                        m.id,
                        m.session_id,
                        m.role,
                        snippet(messages_fts_trigram, 0, '>>>', '<<<', '...', 40) AS snippet,
                        m.content,
                        m.timestamp,
                        m.tool_name,
                        s.source,
                        s.model,
                        s.started_at AS session_started
                    FROM messages_fts_trigram
                    JOIN messages m ON m.id = messages_fts_trigram.rowid
                    JOIN sessions s ON s.id = m.session_id
                    WHERE {' AND '.join(tri_where)}
                    {order_by_sql}
                    LIMIT ? OFFSET ?
                """
                tri_params.extend([limit, offset])
                with self._lock:
                    try:
                        tri_cursor = self._conn.execute(tri_sql, tri_params)
                    except sqlite3.OperationalError:
                        matches = []
                    else:
                        matches = [dict(row) for row in tri_cursor.fetchall()]
            else:
                # Short / mixed CJK query: trigram cannot match tokens with
                # <3 CJK chars. Fall back to LIKE substring search.
                # For multi-token OR queries (e.g. "广西 OR 桂林 OR 漓江"),
                # build one LIKE condition per non-operator token so each term
                # is matched independently (#20494).
                non_op_tokens = [
                    t for t in raw_query.split()
                    if t.upper() not in {"AND", "OR", "NOT"}
                ] or [raw_query]
                token_clauses = []
                like_params: list = []
                for tok in non_op_tokens:
                    esc = tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    token_clauses.append(
                        "(m.content LIKE ? ESCAPE '\\' OR m.tool_name LIKE ? ESCAPE '\\' OR m.tool_calls LIKE ? ESCAPE '\\')"
                    )
                    like_params += [f"%{esc}%", f"%{esc}%", f"%{esc}%"]
                like_where = [f"({' OR '.join(token_clauses)})"]
                if not include_inactive:
                    like_where.append("m.active = 1")
                if source_filter is not None:
                    like_where.append(f"s.source IN ({','.join('?' for _ in source_filter)})")
                    like_params.extend(source_filter)
                if exclude_sources is not None:
                    like_where.append(f"s.source NOT IN ({','.join('?' for _ in exclude_sources)})")
                    like_params.extend(exclude_sources)
                if role_filter:
                    like_where.append(f"m.role IN ({','.join('?' for _ in role_filter)})")
                    like_params.extend(role_filter)
                like_sql = f"""
                    SELECT m.id, m.session_id, m.role,
                           substr(m.content,
                                  max(1, instr(m.content, ?) - 40),
                                  120) AS snippet,
                           m.content, m.timestamp, m.tool_name,
                           s.source, s.model, s.started_at AS session_started
                    FROM messages m
                    JOIN sessions s ON s.id = m.session_id
                    WHERE {' AND '.join(like_where)}
                    ORDER BY m.timestamp DESC
                    LIMIT ? OFFSET ?
                """
                like_params.extend([limit, offset])
                # instr() for snippet uses first search token
                like_params = [non_op_tokens[0]] + like_params
                with self._lock:
                    like_cursor = self._conn.execute(like_sql, like_params)
                    matches = [dict(row) for row in like_cursor.fetchall()]
        else:
            with self._lock:
                try:
                    cursor = self._conn.execute(sql, params)
                except sqlite3.OperationalError:
                    # FTS5 query syntax error despite sanitization — return empty
                    return []
                else:
                    matches = [dict(row) for row in cursor.fetchall()]

        # Add surrounding context (1 message before + after each match).
        # Done outside the lock so we don't hold it across N sequential queries.
        for match in matches:
            try:
                with self._lock:
                    target_active_clause = "" if include_inactive else " AND active = 1"
                    neighbor_active_clause = "" if include_inactive else " AND m.active = 1"
                    ctx_cursor = self._conn.execute(
                        f"""WITH target AS (
                               SELECT session_id, timestamp, id
                               FROM messages
                               WHERE id = ?{target_active_clause}
                           )
                           SELECT role, content
                           FROM (
                               SELECT m.id, m.timestamp, m.role, m.content
                               FROM messages m
                               JOIN target t ON t.session_id = m.session_id
                               WHERE (
                                   (m.timestamp < t.timestamp)
                                   OR (m.timestamp = t.timestamp AND m.id < t.id)
                               )
                               {neighbor_active_clause}
                               ORDER BY m.timestamp DESC, m.id DESC
                               LIMIT 1
                           )
                           UNION ALL
                           SELECT role, content
                           FROM messages
                           WHERE id = ?{target_active_clause}
                           UNION ALL
                           SELECT role, content
                           FROM (
                               SELECT m.id, m.timestamp, m.role, m.content
                               FROM messages m
                               JOIN target t ON t.session_id = m.session_id
                               WHERE (
                                   (m.timestamp > t.timestamp)
                                   OR (m.timestamp = t.timestamp AND m.id > t.id)
                               )
                               {neighbor_active_clause}
                               ORDER BY m.timestamp ASC, m.id ASC
                               LIMIT 1
                           )""",
                        (match["id"], match["id"]),
                    )
                    context_msgs = []
                    for r in ctx_cursor.fetchall():
                        raw = r["content"]
                        decoded = self._decode_content(raw)
                        # Multimodal context: render a compact text-only
                        # summary for search previews.
                        if isinstance(decoded, list):
                            text_parts = [
                                p.get("text", "") for p in decoded
                                if isinstance(p, dict) and p.get("type") == "text"
                            ]
                            text = " ".join(t for t in text_parts if t).strip()
                            preview = text or "[multimodal content]"
                        elif isinstance(decoded, str):
                            preview = decoded
                        else:
                            preview = ""
                        context_msgs.append(
                            {"role": r["role"], "content": preview[:200]}
                        )
                match["context"] = context_msgs
            except Exception:
                match["context"] = []

        # Remove full content from result (snippet is enough, saves tokens)
        for match in matches:
            match.pop("content", None)

        return matches

    def search_sessions_by_id(
        self,
        query: str,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Search surfaced sessions by exact/prefix/substring session id.

        Matching checks each surfaced row's id and projected compression root
        id, while ``list_sessions_rich(id_query=...)`` pushes the candidate
        filter into SQL so desktop/web search does not scan every session row.
        """
        needle = (query or "").strip().lower()
        try:
            bounded_limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            bounded_limit = 20
        if not needle:
            return []

        candidates = self.list_sessions_rich(
            limit=max(bounded_limit * 4, bounded_limit),
            offset=0,
            order_by_last_active=True,
            id_query=needle,
        )

        def score(row: Dict[str, Any]) -> int:
            ids = [str(row.get("id") or ""), str(row.get("_lineage_root_id") or "")]
            normalized = [value.lower() for value in ids if value]
            if any(value == needle for value in normalized):
                return 0
            if any(value.startswith(needle) for value in normalized):
                return 1
            return 2

        ranked = sorted(
            enumerate(candidates),
            key=lambda item: (score(item[1]), item[0]),
        )
        return [row for _, row in ranked[:bounded_limit]]

    def search_sessions(
        self,
        source: str = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List sessions, optionally filtered by source.

        Returns rows with the denormalized ``last_active`` list field,
        falling back to ``started_at``, ordered by most-recently-used first.
        """
        select_with_last_active = (
            "SELECT s.*, COALESCE(s.last_active, s.started_at) AS _last_active_summary "
            "FROM sessions s "
        )
        with self._lock:
            if source:
                cursor = self._conn.execute(
                    f"{select_with_last_active}"
                    "WHERE s.source = ? "
                    "ORDER BY _last_active_summary DESC, s.started_at DESC, s.id DESC LIMIT ? OFFSET ?",
                    (source, limit, offset),
                )
            else:
                cursor = self._conn.execute(
                    f"{select_with_last_active}"
                    "ORDER BY _last_active_summary DESC, s.started_at DESC, s.id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            rows = cursor.fetchall()
        sessions = []
        for row in rows:
            session = dict(row)
            session["last_active"] = session.pop(
                "_last_active_summary",
                session.get("last_active") or session.get("started_at") or 0,
            )
            sessions.append(session)
        return sessions

    # =========================================================================
    # Utility
    # =========================================================================

    def session_count(self, source: str = None) -> int:
        """Count sessions, optionally filtered by source."""
        with self._lock:
            if source:
                cursor = self._conn.execute(
                    "SELECT COUNT(*) FROM sessions WHERE source = ?", (source,)
                )
            else:
                cursor = self._conn.execute("SELECT COUNT(*) FROM sessions")
            return cursor.fetchone()[0]

    def message_count(self, session_id: str = None) -> int:
        """Count messages, optionally for a specific session."""
        with self._lock:
            if session_id:
                cursor = self._conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
                )
            else:
                cursor = self._conn.execute("SELECT COUNT(*) FROM messages")
            return cursor.fetchone()[0]

    # =========================================================================
    # Export and cleanup
    # =========================================================================

    def export_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Export a single session with all its messages as a dict."""
        session = self.get_session(session_id)
        if not session:
            return None
        messages = self.get_messages(session_id)
        return {**session, "messages": messages}

    def export_all(self, source: str = None) -> List[Dict[str, Any]]:
        """
        Export all sessions (with messages) as a list of dicts.
        Suitable for writing to a JSONL file for backup/analysis.
        """
        sessions = self.search_sessions(source=source, limit=100000)
        results = []
        for session in sessions:
            messages = self.get_messages(session["id"])
            results.append({**session, "messages": messages})
        return results

    def clear_messages(self, session_id: str) -> None:
        """Delete all messages for a session and reset its counters."""
        def _do(conn):
            conn.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )
            conn.execute(
                "UPDATE sessions SET message_count = 0, tool_call_count = 0, preview = '', last_active = NULL WHERE id = ?",
                (session_id,),
            )
        self._execute_write(_do)

    @staticmethod
    def _remove_session_files(sessions_dir: Optional[Path], session_id: str) -> None:
        """Remove on-disk transcript files for a session.

        Cleans up ``{session_id}.json``, ``{session_id}.jsonl``, and any
        ``request_dump_{session_id}_*.json`` files left by the gateway.
        Silently skips files that don't exist and swallows OSError so a
        filesystem hiccup never blocks a DB operation.
        """
        if sessions_dir is None:
            return
        for suffix in (".json", ".jsonl"):
            p = sessions_dir / f"{session_id}{suffix}"
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        # request_dump files use session_id as a prefix component
        try:
            for p in sessions_dir.glob(f"request_dump_{session_id}_*.json"):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
        except OSError:
            pass

    def delete_session(
        self,
        session_id: str,
        sessions_dir: Optional[Path] = None,
    ) -> bool:
        """Delete a session and all its messages.

        Child sessions are orphaned (parent_session_id set to NULL) rather
        than cascade-deleted, so they remain accessible independently.
        When *sessions_dir* is provided, also removes on-disk transcript
        files (``.json`` / ``.jsonl`` / ``request_dump_*``) for the deleted
        session. Returns True if the session was found and deleted.
        """
        def _do(conn):
            cursor = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE id = ?", (session_id,)
            )
            if cursor.fetchone()[0] == 0:
                return False
            # Orphan child sessions so FK constraint is satisfied
            conn.execute(
                "UPDATE sessions SET parent_session_id = NULL "
                "WHERE parent_session_id = ?",
                (session_id,),
            )
            conn.execute(
                "UPDATE session_lineage SET parent_session_id = NULL "
                "WHERE parent_session_id = ?",
                (session_id,),
            )
            conn.execute(
                "DELETE FROM session_branch_requests "
                "WHERE source_session_id = ? OR result_session_id = ?",
                (session_id, session_id),
            )
            conn.execute(
                "DELETE FROM session_lineage WHERE session_id = ?",
                (session_id,),
            )
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return True

        deleted = self._execute_write(_do)
        if deleted:
            self._remove_session_files(sessions_dir, session_id)
        return deleted

    def prune_sessions(
        self,
        older_than_days: int = 90,
        source: str = None,
        sessions_dir: Optional[Path] = None,
    ) -> int:
        """Delete sessions older than N days. Returns count of deleted sessions.

        Only prunes ended sessions (not active ones).  Child sessions outside
        the prune window are orphaned (parent_session_id set to NULL) rather
        than cascade-deleted.  When *sessions_dir* is provided, also removes
        on-disk transcript files (``.json`` / ``.jsonl`` /
        ``request_dump_*``) for every pruned session, outside the DB
        transaction.
        """
        cutoff = time.time() - (older_than_days * 86400)
        removed_ids: list[str] = []

        def _do(conn):
            if source:
                cursor = conn.execute(
                    """SELECT id FROM sessions
                       WHERE started_at < ? AND ended_at IS NOT NULL AND source = ?""",
                    (cutoff, source),
                )
            else:
                cursor = conn.execute(
                    "SELECT id FROM sessions WHERE started_at < ? AND ended_at IS NOT NULL",
                    (cutoff,),
                )
            session_ids = {row["id"] for row in cursor.fetchall()}

            if not session_ids:
                return 0

            # Orphan any sessions whose parent is about to be deleted
            placeholders = ",".join("?" * len(session_ids))
            conn.execute(
                f"UPDATE sessions SET parent_session_id = NULL "
                f"WHERE parent_session_id IN ({placeholders})",
                list(session_ids),
            )
            conn.execute(
                f"UPDATE session_lineage SET parent_session_id = NULL "
                f"WHERE parent_session_id IN ({placeholders})",
                list(session_ids),
            )
            conn.execute(
                f"DELETE FROM session_branch_requests "
                f"WHERE source_session_id IN ({placeholders}) "
                f"OR result_session_id IN ({placeholders})",
                list(session_ids) + list(session_ids),
            )
            conn.execute(
                f"DELETE FROM session_lineage "
                f"WHERE session_id IN ({placeholders})",
                list(session_ids),
            )

            for sid in session_ids:
                conn.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
                conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
                removed_ids.append(sid)
            return len(session_ids)

        count = self._execute_write(_do)
        # Clean up on-disk files outside the DB transaction
        for sid in removed_ids:
            self._remove_session_files(sessions_dir, sid)
        return count

    # ── Meta key/value (for scheduler bookkeeping) ──

    def get_meta(self, key: str) -> Optional[str]:
        """Read a value from the state_meta key/value store."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM state_meta WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return row["value"] if isinstance(row, sqlite3.Row) else row[0]

    def set_meta(self, key: str, value: str) -> None:
        """Write a value to the state_meta key/value store."""
        def _do(conn):
            conn.execute(
                "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        self._execute_write(_do)

    def apply_telegram_topic_migration(self) -> None:
        """Create Telegram DM topic-mode tables on explicit /topic opt-in.

        This migration is deliberately not part of automatic SessionDB startup
        reconciliation. Operators must be able to upgrade Hermes, keep the old
        Telegram bot behavior running, and only mutate topic-mode state when the
        user executes /topic to opt into the feature.

        Schema versions:
          v1 — initial shape (no ON DELETE CASCADE on session_id FK)
          v2 — session_id FK gets ON DELETE CASCADE so session pruning
               automatically clears bindings.
        """
        def _do(conn):
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS telegram_dm_topic_mode (
                    chat_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    activated_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    has_topics_enabled INTEGER,
                    allows_users_to_create_topics INTEGER,
                    capability_checked_at REAL,
                    intro_message_id TEXT,
                    pinned_message_id TEXT
                );

                CREATE TABLE IF NOT EXISTS telegram_dm_topic_bindings (
                    chat_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    managed_mode TEXT NOT NULL DEFAULT 'auto',
                    linked_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (chat_id, thread_id)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_telegram_dm_topic_bindings_session
                ON telegram_dm_topic_bindings(session_id);

                CREATE INDEX IF NOT EXISTS idx_telegram_dm_topic_bindings_user
                ON telegram_dm_topic_bindings(user_id, chat_id);
                """
            )

            # v1 → v2: rebuild telegram_dm_topic_bindings if its session_id FK
            # lacks ON DELETE CASCADE. SQLite can't ALTER a foreign key, so we
            # rebuild the table. Only runs once per DB (version gate).
            current = conn.execute(
                "SELECT value FROM state_meta WHERE key = ?",
                ("telegram_dm_topic_schema_version",),
            ).fetchone()
            current_version = int(current[0]) if current and str(current[0]).isdigit() else 0
            if current_version < 2:
                fk_rows = conn.execute(
                    "PRAGMA foreign_key_list('telegram_dm_topic_bindings')"
                ).fetchall()
                needs_rebuild = any(
                    row[2] == "sessions" and (row[6] or "") != "CASCADE"
                    for row in fk_rows
                )
                if needs_rebuild:
                    conn.executescript(
                        """
                        CREATE TABLE telegram_dm_topic_bindings_new (
                            chat_id TEXT NOT NULL,
                            thread_id TEXT NOT NULL,
                            user_id TEXT NOT NULL,
                            session_key TEXT NOT NULL,
                            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                            managed_mode TEXT NOT NULL DEFAULT 'auto',
                            linked_at REAL NOT NULL,
                            updated_at REAL NOT NULL,
                            PRIMARY KEY (chat_id, thread_id)
                        );
                        INSERT INTO telegram_dm_topic_bindings_new
                            SELECT chat_id, thread_id, user_id, session_key,
                                   session_id, managed_mode, linked_at, updated_at
                            FROM telegram_dm_topic_bindings;
                        DROP TABLE telegram_dm_topic_bindings;
                        ALTER TABLE telegram_dm_topic_bindings_new
                            RENAME TO telegram_dm_topic_bindings;
                        CREATE UNIQUE INDEX idx_telegram_dm_topic_bindings_session
                            ON telegram_dm_topic_bindings(session_id);
                        CREATE INDEX idx_telegram_dm_topic_bindings_user
                            ON telegram_dm_topic_bindings(user_id, chat_id);
                        """
                    )

            conn.execute(
                "INSERT INTO state_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("telegram_dm_topic_schema_version", "2"),
            )
        self._execute_write(_do)

    def enable_telegram_topic_mode(
        self,
        *,
        chat_id: str,
        user_id: str,
        has_topics_enabled: Optional[bool] = None,
        allows_users_to_create_topics: Optional[bool] = None,
    ) -> None:
        """Enable Telegram DM topic mode for one private chat/user.

        This method intentionally owns the explicit topic migration. Ordinary
        SessionDB startup must not create these side tables.
        """
        self.apply_telegram_topic_migration()
        now = time.time()

        def _to_int(value: Optional[bool]) -> Optional[int]:
            if value is None:
                return None
            return 1 if value else 0

        def _do(conn):
            conn.execute(
                """
                INSERT INTO telegram_dm_topic_mode (
                    chat_id, user_id, enabled, activated_at, updated_at,
                    has_topics_enabled, allows_users_to_create_topics,
                    capability_checked_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    enabled = 1,
                    updated_at = excluded.updated_at,
                    has_topics_enabled = excluded.has_topics_enabled,
                    allows_users_to_create_topics = excluded.allows_users_to_create_topics,
                    capability_checked_at = excluded.capability_checked_at
                """,
                (
                    str(chat_id),
                    str(user_id),
                    now,
                    now,
                    _to_int(has_topics_enabled),
                    _to_int(allows_users_to_create_topics),
                    now,
                ),
            )
        self._execute_write(_do)

    def disable_telegram_topic_mode(
        self,
        *,
        chat_id: str,
        clear_bindings: bool = True,
    ) -> None:
        """Disable Telegram DM topic mode for one private chat.

        When ``clear_bindings`` is True (default) the (chat_id, thread_id)
        bindings for this chat are also cleared so re-enabling later
        starts from a clean slate. Set to False if the operator wants to
        preserve bindings for a later re-enable.

        Never creates the topic-mode tables from scratch; if they don't
        exist there is nothing to disable and the call is a no-op.
        """
        def _do(conn):
            try:
                conn.execute(
                    "UPDATE telegram_dm_topic_mode SET enabled = 0, updated_at = ? "
                    "WHERE chat_id = ?",
                    (time.time(), str(chat_id)),
                )
                if clear_bindings:
                    conn.execute(
                        "DELETE FROM telegram_dm_topic_bindings WHERE chat_id = ?",
                        (str(chat_id),),
                    )
            except sqlite3.OperationalError:
                # Tables don't exist yet — nothing to disable.
                return
        self._execute_write(_do)

    def is_telegram_topic_mode_enabled(self, *, chat_id: str, user_id: str) -> bool:
        """Return whether Telegram DM topic mode is enabled for this chat/user."""
        with self._lock:
            try:
                row = self._conn.execute(
                    """
                    SELECT enabled FROM telegram_dm_topic_mode
                    WHERE chat_id = ? AND user_id = ?
                    """,
                    (str(chat_id), str(user_id)),
                ).fetchone()
            except sqlite3.OperationalError:
                return False
        if row is None:
            return False
        enabled = row["enabled"] if isinstance(row, sqlite3.Row) else row[0]
        return bool(enabled)

    def get_telegram_topic_binding(
        self,
        *,
        chat_id: str,
        thread_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the session binding for a Telegram DM topic, if present."""
        with self._lock:
            try:
                row = self._conn.execute(
                    """
                    SELECT * FROM telegram_dm_topic_bindings
                    WHERE chat_id = ? AND thread_id = ?
                    """,
                    (str(chat_id), str(thread_id)),
                ).fetchone()
            except sqlite3.OperationalError:
                return None
        return dict(row) if row else None

    def list_telegram_topic_bindings_for_chat(
        self,
        *,
        chat_id: str,
    ) -> List[Dict[str, Any]]:
        """All Telegram DM topic bindings for one chat, newest first.

        Read-only; returns [] if the bindings table doesn't exist yet
        (does not trigger the topic-mode migration).
        """
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT * FROM telegram_dm_topic_bindings "
                    "WHERE chat_id = ? ORDER BY updated_at DESC",
                    (str(chat_id),),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [dict(row) for row in rows]

    def get_telegram_topic_binding_by_session(
        self,
        *,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the Telegram DM topic binding for a given session_id, if present.

        Uses the UNIQUE INDEX on telegram_dm_topic_bindings(session_id) for an
        efficient reverse lookup. Returns None when the session has no binding or
        the table does not exist yet.
        """
        with self._lock:
            try:
                row = self._conn.execute(
                    """
                    SELECT * FROM telegram_dm_topic_bindings
                    WHERE session_id = ?
                    """,
                    (str(session_id),),
                ).fetchone()
            except sqlite3.OperationalError:
                return None
        return dict(row) if row else None

    def bind_telegram_topic(
        self,
        *,
        chat_id: str,
        thread_id: str,
        user_id: str,
        session_key: str,
        session_id: str,
        managed_mode: str = "auto",
    ) -> None:
        """Bind one Telegram DM topic thread to one Hermes session.

        A Hermes session may only be linked to one Telegram topic in MVP.
        Rebinding the same topic to the same session is idempotent; trying to
        link the same session to a different topic raises ValueError.
        """
        self.apply_telegram_topic_migration()
        now = time.time()
        chat_id = str(chat_id)
        thread_id = str(thread_id)
        user_id = str(user_id)
        session_key = str(session_key)
        session_id = str(session_id)

        def _do(conn):
            existing_session = conn.execute(
                """
                SELECT chat_id, thread_id FROM telegram_dm_topic_bindings
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if existing_session is not None:
                linked_chat = existing_session["chat_id"] if isinstance(existing_session, sqlite3.Row) else existing_session[0]
                linked_thread = existing_session["thread_id"] if isinstance(existing_session, sqlite3.Row) else existing_session[1]
                if str(linked_chat) != chat_id or str(linked_thread) != thread_id:
                    raise ValueError("session is already linked to another Telegram topic")

            conn.execute(
                """
                INSERT INTO telegram_dm_topic_bindings (
                    chat_id, thread_id, user_id, session_key, session_id,
                    managed_mode, linked_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, thread_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    session_key = excluded.session_key,
                    session_id = excluded.session_id,
                    managed_mode = excluded.managed_mode,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_id,
                    thread_id,
                    user_id,
                    session_key,
                    session_id,
                    managed_mode,
                    now,
                    now,
                ),
            )
        self._execute_write(_do)

    def is_telegram_session_linked_to_topic(self, *, session_id: str) -> bool:
        """Return True if a Hermes session is already bound to any Telegram DM topic.

        Read-only: does NOT trigger the telegram-topic migration. If the
        topic-mode tables have not been created yet (i.e. nobody has run
        ``/topic`` in this profile), the session is by definition unbound
        and we return False.
        """
        with self._lock:
            try:
                row = self._conn.execute(
                    """
                    SELECT 1 FROM telegram_dm_topic_bindings
                    WHERE session_id = ?
                    LIMIT 1
                    """,
                    (str(session_id),),
                ).fetchone()
            except sqlite3.OperationalError:
                return False
        return row is not None

    def list_unlinked_telegram_sessions_for_user(
        self,
        *,
        chat_id: str,
        user_id: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """List previous Telegram sessions for this user that are not bound to a topic.

        Read-only: does NOT trigger the telegram-topic migration. If the
        topic-mode tables are absent, fall back to a simpler query that
        just returns this user's Telegram sessions — there can't be any
        bindings yet.
        """
        with self._lock:
            try:
                rows = self._conn.execute(
                    """
                    SELECT s.*,
                        COALESCE(s.preview, '') AS _preview_summary,
                        COALESCE(s.last_active, s.started_at) AS _last_active_summary
                    FROM sessions s
                    WHERE s.source = 'telegram'
                      AND s.user_id = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM telegram_dm_topic_bindings b
                          WHERE b.session_id = s.id
                      )
                    ORDER BY _last_active_summary DESC, s.started_at DESC
                    LIMIT ?
                    """,
                    (str(user_id), int(limit)),
                ).fetchall()
            except sqlite3.OperationalError:
                # telegram_dm_topic_bindings doesn't exist yet — no bindings
                # means every telegram session for this user is "unlinked".
                rows = self._conn.execute(
                    """
                    SELECT s.*,
                        COALESCE(s.preview, '') AS _preview_summary,
                        COALESCE(s.last_active, s.started_at) AS _last_active_summary
                    FROM sessions s
                    WHERE s.source = 'telegram'
                      AND s.user_id = ?
                    ORDER BY _last_active_summary DESC, s.started_at DESC
                    LIMIT ?
                    """,
                    (str(user_id), int(limit)),
                ).fetchall()

        sessions: List[Dict[str, Any]] = []
        for row in rows:
            session = dict(row)
            session["preview"] = str(session.pop("_preview_summary", session.get("preview") or "") or "")
            session["last_active"] = session.pop(
                "_last_active_summary",
                session.get("last_active") or session.get("started_at") or 0,
            )
            sessions.append(session)
        return sessions

    # ── Space reclamation ──

    def vacuum(self) -> None:
        """Run VACUUM to reclaim disk space after large deletes.

        SQLite does not shrink the database file when rows are deleted —
        freed pages just get reused on the next insert. After a prune that
        removed hundreds of sessions, the file stays bloated unless we
        explicitly VACUUM.

        VACUUM rewrites the entire DB, so it's expensive (seconds per
        100MB) and cannot run inside a transaction. It also acquires an
        exclusive lock, so callers must ensure no other writers are
        active. Safe to call at startup before the gateway/CLI starts
        serving traffic.
        """
        # VACUUM cannot be executed inside a transaction.
        with self._lock:
            # Best-effort WAL checkpoint first, then VACUUM.
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            self._conn.execute("VACUUM")

    def maybe_auto_prune_and_vacuum(
        self,
        retention_days: int = 90,
        min_interval_hours: int = 24,
        vacuum: bool = True,
        sessions_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Idempotent auto-maintenance: prune old sessions + optional VACUUM.

        Records the last run timestamp in state_meta so subsequent calls
        within ``min_interval_hours`` no-op. Designed to be called once at
        startup from long-lived entrypoints (CLI, gateway, cron scheduler).

        When *sessions_dir* is provided, on-disk transcript files
        (``.json`` / ``.jsonl`` / ``request_dump_*``) for pruned sessions
        are removed as part of the same sweep (issue #3015).

        Never raises. On any failure, logs a warning and returns a dict
        with ``"error"`` set.

        Returns a dict with keys:
          - ``"skipped"`` (bool) — true if within min_interval_hours of last run
          - ``"pruned"`` (int)   — number of sessions deleted
          - ``"vacuumed"`` (bool) — true if VACUUM ran
          - ``"error"`` (str, optional) — present only on failure
        """
        result: Dict[str, Any] = {"skipped": False, "pruned": 0, "vacuumed": False}
        try:
            # Skip if another process/call did maintenance recently.
            last_raw = self.get_meta("last_auto_prune")
            now = time.time()
            if last_raw:
                try:
                    last_ts = float(last_raw)
                    if now - last_ts < min_interval_hours * 3600:
                        result["skipped"] = True
                        return result
                except (TypeError, ValueError):
                    pass  # corrupt meta; treat as no prior run

            pruned = self.prune_sessions(
                older_than_days=retention_days,
                sessions_dir=sessions_dir,
            )
            result["pruned"] = pruned

            # Only VACUUM if we actually freed rows — VACUUM on a tight DB
            # is wasted I/O. Threshold keeps small DBs from paying the cost.
            if vacuum and pruned > 0:
                try:
                    self.vacuum()
                    result["vacuumed"] = True
                except Exception as exc:
                    logger.warning("state.db VACUUM failed: %s", exc)

            # Record the attempt even if pruned == 0, so we don't retry
            # every startup within the min_interval_hours window.
            self.set_meta("last_auto_prune", str(now))

            if pruned > 0:
                logger.info(
                    "state.db auto-maintenance: pruned %d session(s) older than %d days%s",
                    pruned,
                    retention_days,
                    " + VACUUM" if result["vacuumed"] else "",
                )
        except Exception as exc:
            # Maintenance must never block startup. Log and return error marker.
            logger.warning("state.db auto-maintenance failed: %s", exc)
            result["error"] = str(exc)

        return result

    def maybe_auto_compact_run_events(
        self,
        min_interval_hours: int = 24,
        vacuum: bool = True,
    ) -> Dict[str, Any]:
        """Idempotent run-event maintenance for token-stream storage.

        Live streaming emits token-sized events, but the durable run history
        should keep coalesced replay segments. This maintenance is separate
        from session pruning so desktop/profile runtimes can reclaim old
        chunk rows even when session retention pruning is disabled.
        """
        result: Dict[str, Any] = {
            "skipped": False,
            "deleted_events": 0,
            "compacted_segments": 0,
            "deduplicated_terminal_groups": 0,
            "updated_events": 0,
            "vacuumed": False,
        }
        try:
            last_raw = self.get_meta("last_auto_run_event_compaction_v1")
            now = time.time()
            if last_raw:
                try:
                    last_ts = float(last_raw)
                    if now - last_ts < min_interval_hours * 3600:
                        result["skipped"] = True
                        return result
                except (TypeError, ValueError):
                    pass

            compacted = self.compact_run_events()
            result.update({
                "deleted_events": int(compacted.get("deleted_events") or 0),
                "compacted_segments": int(compacted.get("compacted_segments") or 0),
                "deduplicated_terminal_groups": int(compacted.get("deduplicated_terminal_groups") or 0),
                "updated_events": int(compacted.get("updated_events") or 0),
            })
            if vacuum and result["deleted_events"] > 0:
                try:
                    self.vacuum()
                    result["vacuumed"] = True
                except Exception as exc:
                    logger.warning("state.db run-event VACUUM failed: %s", exc)
            self.set_meta("last_auto_run_event_compaction_v1", str(now))
            if result["deleted_events"] > 0:
                logger.info(
                    "state.db run-event maintenance: compacted %d segment(s), deleted %d event row(s)%s",
                    result["compacted_segments"],
                    result["deleted_events"],
                    " + VACUUM" if result["vacuumed"] else "",
                )
        except Exception as exc:
            logger.warning("state.db run-event maintenance failed: %s", exc)
            result["error"] = str(exc)
        return result

    # ── Handoff (cross-platform session transfer) ──────────────────────────
    #
    # State machine:
    #   None       — no handoff in flight
    #   "pending"  — CLI requested handoff, gateway hasn't picked it up yet
    #   "running"  — gateway is processing (session switch + synthetic turn)
    #   "completed"— gateway successfully delivered the synthetic turn
    #   "failed"   — gateway hit an error; reason in handoff_error
    #
    # The CLI writes "pending" then poll-waits for terminal state. The gateway
    # watcher transitions pending→running→{completed,failed}.

    def request_handoff(self, session_id: str, platform: str) -> bool:
        """Mark a session as pending handoff to the given platform.

        Returns True if the row was found and not already in flight; False if
        the session is already in a non-terminal handoff state.
        """
        def _do(conn):
            cur = conn.execute(
                "UPDATE sessions "
                "SET handoff_state = 'pending', "
                "    handoff_platform = ?, "
                "    handoff_error = NULL "
                "WHERE id = ? AND (handoff_state IS NULL "
                "                  OR handoff_state IN ('completed', 'failed'))",
                (platform, session_id),
            )
            return cur.rowcount > 0
        return self._execute_write(_do)

    def get_handoff_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Read the current handoff state for a session.

        Returns ``{"state", "platform", "error"}`` or None if the session has
        no handoff record.
        """
        try:
            cur = self._conn.execute(
                "SELECT handoff_state, handoff_platform, handoff_error "
                "FROM sessions WHERE id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "state": row["handoff_state"],
                "platform": row["handoff_platform"],
                "error": row["handoff_error"],
            }
        except Exception:
            return None

    def list_pending_handoffs(self) -> List[Dict[str, Any]]:
        """Return all sessions in handoff_state='pending', oldest first.

        Used by the gateway's handoff watcher.
        """
        try:
            cur = self._conn.execute(
                "SELECT * FROM sessions "
                "WHERE handoff_state = 'pending' "
                "ORDER BY started_at ASC"
            )
            return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    def claim_handoff(self, session_id: str) -> bool:
        """Atomically transition pending → running. Returns True if claimed."""
        def _do(conn):
            cur = conn.execute(
                "UPDATE sessions SET handoff_state = 'running' "
                "WHERE id = ? AND handoff_state = 'pending'",
                (session_id,),
            )
            return cur.rowcount > 0
        return self._execute_write(_do)

    def complete_handoff(self, session_id: str) -> None:
        """Mark a handoff as completed."""
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET handoff_state = 'completed', "
                "handoff_error = NULL WHERE id = ?",
                (session_id,),
            )
        self._execute_write(_do)

    def fail_handoff(self, session_id: str, error: str) -> None:
        """Mark a handoff as failed and record the reason."""
        def _do(conn):
            conn.execute(
                "UPDATE sessions SET handoff_state = 'failed', "
                "handoff_error = ? WHERE id = ?",
                (error[:500], session_id),
            )
        self._execute_write(_do)
