"""Declarative SQLite schema for Hermes state storage."""

from __future__ import annotations

from hermes_agent.storage.conversation_memory_schema import CONVERSATION_MEMORY_SCHEMA_SQL
from hermes_team_mission.state.schema import team_mission_deferred_index_sql
from hermes_team_mission.state.schema import team_mission_schema_sql

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS applied_migrations (
    version INTEGER PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    applied_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS delivery_obligations (
    obligation_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    thread_id TEXT,
    reply_to TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    content TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'attempting', 'delivered', 'failed', 'abandoned')
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    owner_pid INTEGER,
    owner_started_at INTEGER,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_delivery_obligations_recovery
    ON delivery_obligations(state, updated_at);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    user_id TEXT,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL DEFAULT 0,
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
    cwd TEXT,
    git_branch TEXT NOT NULL DEFAULT '',
    git_repo_root TEXT NOT NULL DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0,
    session_kind TEXT NOT NULL DEFAULT 'hermes_session',
    conversation_kind TEXT NOT NULL DEFAULT 'direct',
    api_call_count INTEGER DEFAULT 0,
    handoff_state TEXT,
    handoff_platform TEXT,
    handoff_error TEXT,
    rewind_count INTEGER NOT NULL DEFAULT 0,
    transient INTEGER DEFAULT 0,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS session_compression_leases (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    holder TEXT NOT NULL,
    expires_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_session_compression_leases_expiry
    ON session_compression_leases(expires_at);

CREATE TABLE IF NOT EXISTS session_runtime_stability (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    compression_ineffective_count INTEGER NOT NULL DEFAULT 0
        CHECK (compression_ineffective_count >= 0),
    compression_fallback_streak INTEGER NOT NULL DEFAULT 0
        CHECK (compression_fallback_streak >= 0),
    compression_verdict_pending INTEGER NOT NULL DEFAULT 0
        CHECK (compression_verdict_pending IN (0, 1)),
    compression_failure_cooldown_until REAL NOT NULL DEFAULT 0,
    compression_failure_error TEXT NOT NULL DEFAULT '',
    stream_stale_failures INTEGER NOT NULL DEFAULT 0
        CHECK (stream_stale_failures >= 0),
    stream_stale_retry_after REAL NOT NULL DEFAULT 0,
    stream_stale_route_hash TEXT NOT NULL DEFAULT '',
    stream_stale_last_error TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS verification_workspace_state (
    scope_id TEXT NOT NULL,
    workspace_root TEXT NOT NULL,
    edit_generation INTEGER NOT NULL DEFAULT 0 CHECK (edit_generation >= 0),
    last_verified_generation INTEGER NOT NULL DEFAULT -1,
    last_event_id INTEGER,
    changed_paths_json TEXT NOT NULL DEFAULT '[]',
    updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (scope_id, workspace_root)
);

CREATE TABLE IF NOT EXISTS verification_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_id TEXT NOT NULL,
    workspace_root TEXT NOT NULL,
    edit_generation INTEGER NOT NULL CHECK (edit_generation >= 0),
    command TEXT NOT NULL,
    canonical_command TEXT NOT NULL,
    kind TEXT NOT NULL,
    evidence_scope TEXT NOT NULL CHECK (evidence_scope IN ('targeted', 'full')),
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed')),
    exit_code INTEGER NOT NULL,
    cwd TEXT NOT NULL,
    output_summary TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_verification_evidence_scope_root
    ON verification_evidence(scope_id, workspace_root, id DESC);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT,
    icon TEXT,
    color TEXT,
    board_slug TEXT,
    primary_path TEXT,
    created_at REAL NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1))
);

CREATE TABLE IF NOT EXISTS project_folders (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    label TEXT,
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
    added_at REAL NOT NULL,
    PRIMARY KEY (project_id, path)
);

CREATE INDEX IF NOT EXISTS idx_project_folders_path ON project_folders(path);

CREATE TABLE IF NOT EXISTS project_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS discovered_repos (
    root TEXT PRIMARY KEY,
    label TEXT,
    last_seen REAL NOT NULL
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
    conversation_kind TEXT NOT NULL DEFAULT 'direct',
    status TEXT NOT NULL DEFAULT 'idle',
    running INTEGER NOT NULL DEFAULT 0,
    waiting_approval INTEGER NOT NULL DEFAULT 0,
    active_run_id TEXT NOT NULL DEFAULT '',
    active_execution_session_id TEXT NOT NULL DEFAULT '',
    pending_approval_count INTEGER NOT NULL DEFAULT 0,
    team_id TEXT NOT NULL DEFAULT '',
    mission_id TEXT NOT NULL DEFAULT '',
    conversation_id TEXT NOT NULL DEFAULT '',
    message_count INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    last_activity REAL
);

CREATE TABLE IF NOT EXISTS conversation_participants (
    conversation_session_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    member_id TEXT NOT NULL DEFAULT '',
    agent_profile_id TEXT NOT NULL DEFAULT '',
    agent_profile_version_id TEXT NOT NULL DEFAULT '',
    runtime_scope_key TEXT NOT NULL DEFAULT '',
    memory_namespace TEXT NOT NULL DEFAULT '',
    transcript_cursor INTEGER NOT NULL DEFAULT 0,
    memory_revision INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive', 'removed')),
    display_name TEXT NOT NULL DEFAULT '',
    avatar TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (conversation_session_id, participant_id)
);

CREATE TABLE IF NOT EXISTS activities (
    activity_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    parent_activity_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('chat', 'agent_dispatch', 'team_dispatch', 'member_chat', 'mission')),
    target_profile_id TEXT,
    target_team_id TEXT,
    target_mission_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    prompt_summary TEXT,
    result_summary TEXT,
    result_json TEXT,
    started_at REAL,
    completed_at REAL,
    notify_parent INTEGER NOT NULL DEFAULT 1,
    read_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS v3_activities (
    activity_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (
        kind IN (
            'async_agent_dispatch',
            'async_team_dispatch',
            'team_mission_activity',
            'dispatch_completion'
        )
    ),
    activity_seq INTEGER NOT NULL CHECK (activity_seq >= 1),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'running', 'completed', 'failed', 'cancelled')
    ),
    target_id TEXT NOT NULL DEFAULT '',
    prompt_summary TEXT,
    result_summary TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL DEFAULT 0,
    completed_at REAL,
    metadata_json TEXT,
    UNIQUE(session_id, activity_seq)
);

CREATE TABLE IF NOT EXISTS activity_commands (
    command_id TEXT PRIMARY KEY,
    activity_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('create', 'start', 'cancel', 'complete')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    intent_at REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'accepted'
        CHECK (state IN ('accepted', 'dispatched', 'satisfied', 'failed')),
    state_changed_at REAL NOT NULL,
    result_event_id INTEGER,
    error_reason TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (result_event_id) REFERENCES run_events(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_activity_commands_state
    ON activity_commands(state, intent_at);
CREATE INDEX IF NOT EXISTS idx_activity_commands_activity
    ON activity_commands(activity_id, intent_at);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    participant_id TEXT NOT NULL DEFAULT '',
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
    conversation_message_id TEXT NOT NULL DEFAULT '',
    metadata_json TEXT,
    api_content TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS session_system_prompts (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    scope_key TEXT NOT NULL,
    system_prompt TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (session_id, scope_key)
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
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    runtime_scope_key TEXT,
    worker_id TEXT NOT NULL DEFAULT '',
    agent_profile_id TEXT NOT NULL DEFAULT '',
    turn_id TEXT,
    execution_session_id TEXT,
    status TEXT NOT NULL,
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL,
    last_seq INTEGER DEFAULT 0,
    terminal_seq INTEGER NOT NULL DEFAULT 0,
    terminal_degraded INTEGER NOT NULL DEFAULT 0,
    terminal_cause TEXT NOT NULL DEFAULT '',
    error TEXT,
    metadata_json TEXT
);

CREATE TABLE IF NOT EXISTS seq_counter (
    session_id TEXT PRIMARY KEY,
    next_seq INTEGER NOT NULL CHECK (next_seq >= 1),
    updated_at REAL NOT NULL DEFAULT 0,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    run_id TEXT,
    turn_id TEXT,
    execution_session_id TEXT,
    runtime_scope_key TEXT,
    participant_id TEXT NOT NULL DEFAULT '',
    activity_id TEXT,
    event_type TEXT NOT NULL,
    seq INTEGER NOT NULL,
    timestamp REAL NOT NULL,
    payload_json TEXT,
    event_json TEXT NOT NULL,
    status TEXT,
    frame_blob BLOB,
    frame_format TEXT,
    retention_class TEXT,
    projected_message_id TEXT,
    projected_tool_event_id TEXT,
    interaction_request_id TEXT,
    interaction_kind TEXT,
    interaction_status TEXT,
    anchor_seq INTEGER NOT NULL DEFAULT 0,
    projection_state TEXT,
    runtime_source_seq INTEGER NOT NULL DEFAULT 0,
    UNIQUE(session_id, seq)
);

-- idx_run_events_activity_seq is created via DEFERRED_INDEX_SQL after
-- _reconcile_columns() has added the activity_id column to legacy DBs.
-- Declaring it here would break startup on any DB that predates ADR-0001
-- Phase 0 (the index's column reference fails before the reconciler runs).

CREATE TABLE IF NOT EXISTS run_event_search_index (
    run_event_id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    runtime_scope_key TEXT,
    runtime_source_seq INTEGER NOT NULL DEFAULT 0,
    search_text TEXT NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (run_event_id) REFERENCES run_events(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS session_runtime_state (
    session_id TEXT PRIMARY KEY,
    runtime_scope_key TEXT,
    execution_session_id TEXT,
    run_id TEXT,
    turn_id TEXT,
    status TEXT,
    model TEXT,
    provider TEXT,
    profile_json TEXT,
    payload_hash TEXT,
    updated_at REAL NOT NULL,
    source_seq INTEGER
);

CREATE TABLE IF NOT EXISTS tool_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    run_id TEXT,
    turn_id TEXT,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at REAL,
    updated_at REAL NOT NULL,
    completed_at REAL,
    seq_start INTEGER,
    seq_last INTEGER,
    arguments_json TEXT,
    progress_json TEXT,
    result_json TEXT,
    result_text TEXT,
    summary TEXT,
    participant_id TEXT NOT NULL DEFAULT '',
    metadata_json TEXT,
    UNIQUE(session_id, tool_call_id)
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
    source_kind TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
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
""" + team_mission_schema_sql() + CONVERSATION_MEMORY_SCHEMA_SQL + """
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

# Cron history is queried by one source/id prefix. Keep this reusable because
# both the full schema bootstrap and the repository-only bootstrap own a path
# into the same canonical sessions table.
CRON_RUN_HISTORY_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_sessions_source_id
    ON sessions(source, id);
"""

# Indexes must be created after _reconcile_columns() runs. SQLite parses index
# definitions immediately; if an existing table is missing an indexed column,
# CREATE INDEX fails before the reconciler can add that column.
DEFERRED_INDEX_SQL = CRON_RUN_HISTORY_INDEX_SQL + """
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
-- ADR-0001 Phase 0: activity_id is reconciler-added on legacy DBs, so this
-- partial index must run AFTER _reconcile_columns(), i.e. via DEFERRED_INDEX_SQL.
CREATE INDEX IF NOT EXISTS idx_run_events_activity_seq
    ON run_events(activity_id, seq)
    WHERE activity_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_messages_session
    ON messages(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_session_active
    ON messages(session_id, active, timestamp);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_conversation_message_id
    ON messages(session_id, conversation_message_id)
    WHERE conversation_message_id != '';
CREATE INDEX IF NOT EXISTS idx_activities_conv
    ON activities(conversation_id, status);
CREATE INDEX IF NOT EXISTS idx_activities_parent
    ON activities(parent_activity_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_activities_mission
    ON activities(target_mission_id)
    WHERE kind = 'mission' AND COALESCE(target_mission_id, '') != '';
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
CREATE INDEX IF NOT EXISTS idx_run_events_participant
    ON run_events(participant_id);
CREATE INDEX IF NOT EXISTS idx_run_events_retention_class
    ON run_events(retention_class, timestamp);
CREATE INDEX IF NOT EXISTS idx_run_events_projection_state
    ON run_events(projection_state, session_id, seq);
CREATE INDEX IF NOT EXISTS idx_run_events_runtime_source_seq
    ON run_events(session_id, runtime_source_seq, event_type);
CREATE INDEX IF NOT EXISTS idx_run_events_interaction_request
    ON run_events(interaction_request_id, seq)
    WHERE interaction_request_id IS NOT NULL AND interaction_request_id != '';
CREATE INDEX IF NOT EXISTS idx_run_events_interaction_pending
    ON run_events(session_id, interaction_status, seq)
    WHERE interaction_request_id IS NOT NULL AND interaction_request_id != '';
CREATE INDEX IF NOT EXISTS idx_run_event_search_index_session_seq
    ON run_event_search_index(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_run_event_search_index_source_seq
    ON run_event_search_index(session_id, runtime_source_seq, event_type);
CREATE INDEX IF NOT EXISTS idx_session_runtime_state_scope
    ON session_runtime_state(runtime_scope_key, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_session_runtime_state_status
    ON session_runtime_state(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_events_session_seq
    ON tool_events(session_id, seq_start, seq_last);
CREATE INDEX IF NOT EXISTS idx_tool_events_run
    ON tool_events(run_id, seq_start);
CREATE INDEX IF NOT EXISTS idx_tool_events_participant
    ON tool_events(participant_id);
CREATE INDEX IF NOT EXISTS idx_v3_activities_session_seq
    ON v3_activities(session_id, activity_seq);
CREATE INDEX IF NOT EXISTS idx_v3_activities_kind_status_seq
    ON v3_activities(kind, status, activity_seq);
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
""" + team_mission_deferred_index_sql() + """
CREATE INDEX IF NOT EXISTS idx_team_capability_snapshots_team
    ON team_capability_snapshots(team_id, version DESC);
CREATE INDEX IF NOT EXISTS idx_team_capability_snapshot_bindings_mission
    ON team_capability_snapshot_bindings(mission_id);
CREATE INDEX IF NOT EXISTS idx_team_capability_snapshot_bindings_conversation
    ON team_capability_snapshot_bindings(conversation_id);
"""

# Repository-owned runtime storage can be bootstrapped independently from the
# legacy aggregate schema. Keep its deferred indexes executable without first
# reconciling unrelated table families such as session_lineage.
RUNTIME_DEFERRED_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_run_events_activity_seq
    ON run_events(activity_id, seq)
    WHERE activity_id IS NOT NULL;
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
CREATE INDEX IF NOT EXISTS idx_run_events_participant
    ON run_events(participant_id);
CREATE INDEX IF NOT EXISTS idx_run_events_retention_class
    ON run_events(retention_class, timestamp);
CREATE INDEX IF NOT EXISTS idx_run_events_projection_state
    ON run_events(projection_state, session_id, seq);
CREATE INDEX IF NOT EXISTS idx_run_events_runtime_source_seq
    ON run_events(session_id, runtime_source_seq, event_type);
CREATE INDEX IF NOT EXISTS idx_run_events_interaction_request
    ON run_events(interaction_request_id, seq)
    WHERE interaction_request_id IS NOT NULL AND interaction_request_id != '';
CREATE INDEX IF NOT EXISTS idx_run_events_interaction_pending
    ON run_events(session_id, interaction_status, seq)
    WHERE interaction_request_id IS NOT NULL AND interaction_request_id != '';
CREATE INDEX IF NOT EXISTS idx_run_event_search_index_session_seq
    ON run_event_search_index(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_run_event_search_index_source_seq
    ON run_event_search_index(session_id, runtime_source_seq, event_type);
CREATE INDEX IF NOT EXISTS idx_session_runtime_state_scope
    ON session_runtime_state(runtime_scope_key, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_session_runtime_state_status
    ON session_runtime_state(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_events_session_seq
    ON tool_events(session_id, seq_start, seq_last);
CREATE INDEX IF NOT EXISTS idx_tool_events_run
    ON tool_events(run_id, seq_start);
CREATE INDEX IF NOT EXISTS idx_tool_events_participant
    ON tool_events(participant_id);
CREATE INDEX IF NOT EXISTS idx_run_event_archives_session
    ON run_event_archives(session_id, archived_at DESC);
"""
