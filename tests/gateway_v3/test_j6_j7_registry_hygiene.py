"""spec §J6 + §J7 — Registry hygiene.

§J6 Single registry: after full daemon wiring, the entire method surface
lives in exactly one MethodRegistry and passes ``.validate()``.

§J7 Wire naming: every registered method name must follow the wire
convention ``<namespace>.<method>`` where both parts are snake_case with
no uppercase letters, dashes, or camelCase. Names like ``sessionList``
(camelCase) or ``session.List`` (Pascal) or ``session-list`` (kebab) are
rejected — the wire must be consistent snake_case (spec §J7).
"""

from __future__ import annotations

import re
import sqlite3

from hermes_agent.transport.stdio_daemon import build_registry_and_router


_MINIMAL_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT,
    display_title TEXT, display_title_source TEXT,
    session_kind TEXT NOT NULL DEFAULT 'hermes_session',
    conversation_kind TEXT NOT NULL DEFAULT 'direct',
    parent_session_id TEXT, started_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0, ended_at REAL, end_reason TEXT
);
CREATE TABLE session_index (
    session_id TEXT PRIMARY KEY,
    owner_agent_profile_id TEXT NOT NULL DEFAULT '',
    owner_profile_version_id TEXT NOT NULL DEFAULT '',
    runtime_scope_key TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '', preview TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '', session_kind TEXT NOT NULL DEFAULT '',
    conversation_kind TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'idle',
    running INTEGER NOT NULL DEFAULT 0,
    waiting_approval INTEGER NOT NULL DEFAULT 0,
    active_run_id TEXT NOT NULL DEFAULT '',
    active_runtime_session_id TEXT NOT NULL DEFAULT '',
    pending_approval_count INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0, last_activity REAL
);
CREATE TABLE session_branches (
    child_session_id TEXT PRIMARY KEY, parent_session_id TEXT NOT NULL,
    branch_from_seq INTEGER NOT NULL, created_at REAL NOT NULL
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
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
    role TEXT NOT NULL, content TEXT, participant_id TEXT NOT NULL DEFAULT '',
    tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
    timestamp REAL NOT NULL, reasoning TEXT,
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
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
    session_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE seq_counter (
    session_id TEXT PRIMARY KEY, next_seq INTEGER NOT NULL DEFAULT 0
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


def _make_registry():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_MINIMAL_SCHEMA)
    conn.commit()
    registry, _ = build_registry_and_router(
        conn, in_stream=None, out_stream=None  # type: ignore[arg-type]
    )
    return registry


# The wire naming rule: two-part snake_case joined by dot.
# - namespace: [a-z][a-z0-9_]*
# - method:    [a-z][a-z0-9_]*
# - separator: single dot
_WIRE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


# ---------------------------------------------------------------------------


def test_j6_full_daemon_registry_validates():
    """spec §J6 — after full wiring, the registry passes ``.validate()``
    without throwing. This confirms every handler still has its
    ``@requires_permission`` at process-start-up time (not just at
    ``register()`` time).
    """
    registry = _make_registry()
    # Should not raise.
    registry.validate()


def test_j7_every_registered_name_matches_wire_convention():
    """spec §J7 — every registered method name is <namespace>.<method>
    in strict snake_case. No camelCase, no dashes, no PascalCase.
    """
    registry = _make_registry()
    offenders: list[str] = []
    for name in registry.names():
        if not _WIRE_NAME_RE.match(name):
            offenders.append(name)
    if offenders:
        raise AssertionError(
            "spec §J7 wire naming violated — snake_case dot form required:\n"
            + "\n".join(f"  {n!r}" for n in offenders)
        )


def test_j7_no_reserved_python_names_on_the_wire():
    """Method names on the wire must not collide with common Python
    keywords / builtins that would trip an intermediary code generator.
    """
    registry = _make_registry()
    reserved = {
        "class",
        "def",
        "for",
        "if",
        "in",
        "is",
        "not",
        "or",
        "and",
        "return",
        "yield",
        "import",
        "from",
        "as",
        "None",
        "True",
        "False",
        "type",
        "id",
    }
    offenders: list[str] = []
    for name in registry.names():
        namespace, _, method = name.partition(".")
        if namespace in reserved or method in reserved:
            offenders.append(name)
    if offenders:
        raise AssertionError(
            "wire method name collides with Python reserved word:\n"
            + "\n".join(f"  {n!r}" for n in offenders)
        )


def test_j6_registry_contains_expected_namespaces():
    """spec §J6 — the registry surface groups methods into stable
    namespaces. Bumping this list is a wire contract change.
    """
    registry = _make_registry()
    namespaces = {name.split(".", 1)[0] for name in registry.names()}
    expected = {
        "session",
        "run",
        "message",
        "team_mission",
        "agent_profile",
        "system",  # only system.handshake for now
    }
    missing = expected - namespaces
    unknown = namespaces - expected
    if missing or unknown:
        raise AssertionError(
            "namespace drift — "
            f"missing={sorted(missing)!r}  unknown={sorted(unknown)!r}"
        )


def test_j6_duplicate_registration_is_rejected_by_registry():
    """Regression: the registry rejects a second registration of the
    same name (structural §J6 guarantee).
    """
    from hermes_agent.gateway import MethodRegistry, requires_permission
    from hermes_agent.gateway.registry import RegistryError

    @requires_permission("test.duplicate", read_only=True)
    def _handler(params, ctx):
        return {}

    reg = MethodRegistry()
    reg.register("dupe.method", _handler)
    try:
        reg.register("dupe.method", _handler)
    except RegistryError as exc:
        assert "dupe.method" in str(exc)
    else:
        raise AssertionError(
            "registry accepted duplicate registration — §J6 violated"
        )
