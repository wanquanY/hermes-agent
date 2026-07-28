"""Session repository contracts and immutable projections."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol, runtime_checkable


@dataclass(frozen=True)
class SessionSpec:
    """Creation payload for a new session (spec §4.1)."""

    session_id: str
    source: str
    title: str = ""
    display_title: str = ""
    display_title_source: str = ""
    user_id: str = ""
    model: str = ""
    model_config: dict[str, Any] | str | None = None
    transient: bool = False
    session_kind: str = "hermes_session"
    conversation_kind: str = "direct"
    owner_agent_profile_id: str = ""
    owner_profile_version_id: str = ""
    runtime_scope_key: str = ""
    parent_session_id: str = ""


@dataclass(frozen=True)
class Session:
    """Session projection returned by the repo. Read-only snapshot."""

    session_id: str
    source: str
    title: str
    display_title: str
    session_kind: str
    conversation_kind: str
    started_at: float
    updated_at: float
    ended_at: float | None = None
    parent_session_id: str = ""


@dataclass(frozen=True)
class SessionFilter:
    """Filter for list()."""

    source: str | None = None
    session_kind: str | None = None
    conversation_kind: str | None = None
    include_ended: bool = False
    limit: int = 100


@dataclass(frozen=True)
class SessionIndexPatch:
    """Partial update to session_index. Missing fields leave the value unchanged."""

    title: str | None = None
    preview: str | None = None
    status: str | None = None
    running: int | None = None
    waiting_approval: int | None = None
    active_run_id: str | None = None
    active_execution_session_id: str | None = None
    pending_approval_count: int | None = None
    message_count: int | None = None
    last_activity: float | None = None
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionMessageAppendProjection:
    """Session-owned projection update for one appended transcript message."""

    timestamp: float
    tool_call_count: int = 0
    user_preview: str = ""
    user_display_title: str = ""


@dataclass(frozen=True)
class SessionMessageSnapshotProjection:
    """Session-owned projection replacement for a rewritten transcript."""

    message_count: int
    tool_call_count: int
    first_user_preview: str = ""
    first_user_display_title: str = ""
    last_message_ts: float | None = None


@dataclass(frozen=True)
class SessionRunProjection:
    """Session-index projection for a run lifecycle state."""

    session_id: str
    run_id: str
    conversation_session_id: str = ""
    conversation_scope_key: str = ""
    clear_team_mission_rows: bool = False
    execution_session_id: str = ""
    runtime_scope_key: str = ""
    status: str = "running"
    updated_at: float = 0.0


@dataclass(frozen=True)
class BranchSpec:
    """Payload for branch() — spawn a child session from a source."""

    new_session_id: str
    branch_from_seq: int
    title: str = ""
    display_title: str = ""


@dataclass(frozen=True)
class MaterializedBranchSessionSpec:
    new_session_id: str
    title: str
    created_at: float
    message_count: int
    tool_call_count: int
    source_row: Any
    model: str | None = None
    model_config: dict[str, Any] | None = None


@dataclass(frozen=True)
class BranchLineageSpec:
    session_id: str
    parent_session_id: str
    root_session_id: str
    branch_from_message_row_id: int
    branch_from_turn_id: str = ""
    branch_from_run_id: str = ""
    branch_from_client_message_id: str = ""
    branch_origin: str = "user_message_action"
    branch_mode: str = "materialized_prefix"
    branch_depth: int = 1
    created_at: float = 0.0


@dataclass(frozen=True)
class BranchRequestSpec:
    idempotency_key: str
    source_session_id: str
    branch_fingerprint: str
    result_session_id: str
    created_at: float


class SessionNotFound(LookupError):
    """Raised when a session_id has no corresponding row."""


@runtime_checkable
class SessionRepo(Protocol):
    """Aggregate root for the ``sessions`` table family.

    Owned tables: ``sessions``, ``session_index``, ``session_branches``,
    ``session_handoffs``, ``session_lineage``, ``session_branch_requests``.
    """

    def create(self, spec: SessionSpec) -> Session: ...

    def get(self, session_id: str) -> Session | None: ...

    def list(self, filter: SessionFilter) -> Iterable[Session]: ...

    def update_index(self, session_id: str, patch: SessionIndexPatch) -> None: ...

    def get_title(self, session_id: str) -> str | None: ...

    def get_by_title(self, title: str) -> Session | None: ...

    def set_title(self, session_id: str, title: str, *, title_source: str = "user") -> bool: ...

    def update_cwd(self, session_id: str, cwd: str) -> bool: ...

    def update_git_context(
        self,
        session_id: str,
        *,
        branch: str,
        repo_root: str,
    ) -> bool: ...

    def backfill_repo_roots(self, cwd_to_root: dict[str, str]) -> int: ...

    def update_usage(self, session_id: str, fields: dict[str, Any]) -> bool: ...

    def update_runtime_config(
        self,
        session_id: str,
        model_config: dict[str, Any],
        model: str | None = None,
    ) -> bool: ...

    def update_source(self, session_id: str, source: str) -> int: ...

    def update_token_counts(
        self,
        session_id: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str | None = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
        estimated_cost_usd: float | None = None,
        actual_cost_usd: float | None = None,
        cost_status: str | None = None,
        cost_source: str | None = None,
        pricing_version: str | None = None,
        billing_provider: str | None = None,
        billing_base_url: str | None = None,
        billing_mode: str | None = None,
        api_call_count: int = 0,
        absolute: bool = False,
    ) -> None: ...

    def set_archived(self, session_id: str, archived: bool) -> bool: ...

    def update_system_prompt(self, session_id: str, system_prompt: str) -> bool: ...

    def request_handoff(self, session_id: str, platform: str) -> bool: ...

    def list_pending_handoffs(self) -> list[dict[str, Any]]: ...

    def claim_handoff(self, session_id: str) -> bool: ...

    def complete_handoff(self, session_id: str) -> None: ...

    def fail_handoff(self, session_id: str, error: str) -> bool: ...

    def finalize_orphaned_compression_sessions(self) -> int: ...

    def prune_empty_ghost_sessions(self, *, cutoff: float) -> list[str]: ...

    def ensure_runtime_session(self, session_id: str, *, started_at: float | None = None) -> bool: ...

    def ensure_session_record(
        self,
        session_id: str,
        source: str,
        *,
        model: str | None = None,
        model_config: dict[str, Any] | None = None,
        system_prompt: str | None = None,
        user_id: str | None = None,
        parent_session_id: str | None = None,
        transient: bool = False,
        title: str | None = None,
        cwd: str | None = None,
        archived: bool = False,
        session_kind: str = "hermes_session",
        conversation_kind: str = "direct",
    ) -> bool: ...

    def exists(self, session_id: str) -> bool: ...

    def orphan_child_references(self, parent_session_id: str) -> None: ...

    def delete_branch_references(self, session_id: str) -> None: ...

    def repair_orphaned_branch_references(self) -> int: ...

    def delete_row(self, session_id: str) -> bool: ...

    def delete_index(self, session_id: str) -> bool: ...

    def create_materialized_branch_session(self, spec: MaterializedBranchSessionSpec) -> bool: ...

    def record_branch_lineage(self, spec: BranchLineageSpec) -> None: ...

    def record_branch_request(self, spec: BranchRequestSpec) -> None: ...

    def record_message_append(
        self,
        session_id: str,
        projection: SessionMessageAppendProjection,
    ) -> None: ...

    def replace_message_projection(
        self,
        session_id: str,
        projection: SessionMessageSnapshotProjection,
    ) -> None: ...

    def reset_message_projection(self, session_id: str) -> None: ...

    def touch_message_activity(self, session_id: str, timestamp: float) -> None: ...

    def project_runtime_state_event(self, event: dict[str, Any]) -> None: ...

    def project_run_state(self, projection: SessionRunProjection) -> None: ...

    def resolve_resume_session_id(self, session_id: str) -> str: ...

    def branch(self, source_id: str, spec: BranchSpec) -> Session: ...

    def close(self, session_id: str, reason: str) -> None: ...

    def reopen(self, session_id: str) -> None: ...

    def normalize_index_conversation_kind(self) -> int: ...

    def classify_internal_execution(self, session_id: str) -> bool: ...

    def increment_rewind_count(self, session_id: str) -> bool: ...


__all__ = [
    "BranchLineageSpec",
    "BranchRequestSpec",
    "BranchSpec",
    "MaterializedBranchSessionSpec",
    "Session",
    "SessionFilter",
    "SessionIndexPatch",
    "SessionMessageAppendProjection",
    "SessionMessageSnapshotProjection",
    "SessionNotFound",
    "SessionRepo",
    "SessionRunProjection",
    "SessionSpec",
]
