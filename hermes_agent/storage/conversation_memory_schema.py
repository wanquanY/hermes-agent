"""Canonical schema for participant-aware conversation memory and summaries."""

from __future__ import annotations


CONVERSATION_MEMORY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversation_memory_items (
    memory_id TEXT PRIMARY KEY,
    conversation_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('profile', 'participant', 'conversation', 'activity', 'node')),
    owner_id TEXT NOT NULL,
    participant_id TEXT NOT NULL DEFAULT '',
    activity_id TEXT NOT NULL DEFAULT '',
    node_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK (kind IN ('fact', 'preference', 'decision', 'commitment', 'constraint', 'risk', 'open_question', 'artifact', 'summary')),
    content TEXT NOT NULL,
    structured_payload_json TEXT NOT NULL DEFAULT '{}',
    visibility_json TEXT NOT NULL DEFAULT '{}',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    supersedes_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0 CHECK (confidence >= 0 AND confidence <= 1),
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'committed', 'superseded', 'invalidated')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    valid_from REAL NOT NULL DEFAULT 0,
    valid_until REAL,
    invalidated_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversation_memory_owner
    ON conversation_memory_items(owner_kind, owner_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_memory_conversation
    ON conversation_memory_items(conversation_session_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_memory_participant
    ON conversation_memory_items(conversation_session_id, participant_id, status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_memory_activity
    ON conversation_memory_items(activity_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS conversation_memory_edges (
    edge_id TEXT PRIMARY KEY,
    from_memory_id TEXT NOT NULL REFERENCES conversation_memory_items(memory_id) ON DELETE CASCADE,
    to_memory_id TEXT,
    relation TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversation_memory_edges_from
    ON conversation_memory_edges(from_memory_id, relation, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_memory_edges_to
    ON conversation_memory_edges(to_memory_id, relation, created_at DESC);

CREATE TABLE IF NOT EXISTS actor_context_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    conversation_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    actor_participant_id TEXT NOT NULL,
    execution_scope_key TEXT NOT NULL,
    activity_id TEXT NOT NULL DEFAULT '',
    activity_kind TEXT NOT NULL DEFAULT 'chat',
    node_id TEXT NOT NULL DEFAULT '',
    attempt_id TEXT NOT NULL DEFAULT '',
    conversation_revision INTEGER NOT NULL DEFAULT 0 CHECK (conversation_revision >= 0),
    participant_memory_revision INTEGER NOT NULL DEFAULT 0 CHECK (participant_memory_revision >= 0),
    activity_context_revision INTEGER NOT NULL DEFAULT 0 CHECK (activity_context_revision >= 0),
    transcript_cursor INTEGER NOT NULL DEFAULT 0 CHECK (transcript_cursor >= 0),
    selected_event_ids_json TEXT NOT NULL DEFAULT '[]',
    selected_memory_ids_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_actor_context_snapshots_actor
    ON actor_context_snapshots(conversation_session_id, actor_participant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_actor_context_snapshots_activity
    ON actor_context_snapshots(activity_id, created_at DESC);

CREATE TABLE IF NOT EXISTS activity_context_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    conversation_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    activity_id TEXT NOT NULL,
    activity_context_revision INTEGER NOT NULL DEFAULT 1 CHECK (activity_context_revision >= 1),
    objective TEXT NOT NULL,
    conversation_revision INTEGER NOT NULL DEFAULT 0 CHECK (conversation_revision >= 0),
    selected_event_ids_json TEXT NOT NULL DEFAULT '[]',
    selected_memory_ids_json TEXT NOT NULL DEFAULT '[]',
    team_snapshot_json TEXT NOT NULL DEFAULT '{}',
    workspace_snapshot_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'invalidated')),
    supersedes_snapshot_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE(activity_id, activity_context_revision)
);

CREATE INDEX IF NOT EXISTS idx_activity_context_snapshots_active
    ON activity_context_snapshots(activity_id, status, activity_context_revision DESC);

CREATE TABLE IF NOT EXISTS actor_context_summaries (
    summary_id TEXT PRIMARY KEY,
    conversation_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    actor_participant_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL REFERENCES actor_context_snapshots(snapshot_id) ON DELETE CASCADE,
    from_seq INTEGER NOT NULL CHECK (from_seq >= 0),
    to_seq INTEGER NOT NULL CHECK (to_seq >= from_seq),
    conversation_revision INTEGER NOT NULL DEFAULT 0,
    participant_memory_revision INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT NOT NULL,
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'invalidated')),
    supersedes_summary_id TEXT NOT NULL DEFAULT '',
    invalidated_reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(conversation_session_id, actor_participant_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_actor_context_summaries_active
    ON actor_context_summaries(conversation_session_id, actor_participant_id, status, revision DESC);

CREATE TABLE IF NOT EXISTS activity_context_summaries (
    summary_id TEXT PRIMARY KEY,
    conversation_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    activity_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL REFERENCES actor_context_snapshots(snapshot_id) ON DELETE CASCADE,
    activity_context_revision INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT NOT NULL,
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    source_memory_ids_json TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'invalidated')),
    supersedes_summary_id TEXT NOT NULL DEFAULT '',
    invalidated_reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(activity_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_activity_context_summaries_active
    ON activity_context_summaries(activity_id, status, revision DESC);

CREATE TABLE IF NOT EXISTS node_attempt_summaries (
    summary_id TEXT PRIMARY KEY,
    conversation_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    activity_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL REFERENCES actor_context_snapshots(snapshot_id) ON DELETE CASCADE,
    node_attempt_revision INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT NOT NULL,
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'invalidated')),
    supersedes_summary_id TEXT NOT NULL DEFAULT '',
    invalidated_reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(activity_id, node_id, attempt_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_node_attempt_summaries_active
    ON node_attempt_summaries(activity_id, node_id, attempt_id, status, revision DESC);

CREATE TABLE IF NOT EXISTS context_compaction_leases (
    scope_key TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_context_compaction_leases_expiry
    ON context_compaction_leases(expires_at);
"""


__all__ = ["CONVERSATION_MEMORY_SCHEMA_SQL"]
