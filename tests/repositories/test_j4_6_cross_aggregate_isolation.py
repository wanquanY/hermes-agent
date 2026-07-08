"""spec §4.6 — 跨聚合根禁止.

Every RepoImpl may only touch tables owned by its aggregate. This static
test scans each impl's SQL statements and fails if any references a
table owned by another aggregate.

If two aggregates need to cooperate, that goes through the L2 domain
services or L3 orchestration — never as a joint SQL statement inside a
single Repo.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from hermes_agent.repositories import (
    AgentProfileRepoImpl,
    MessageRepoImpl,
    RunRepoImpl,
    SessionRepoImpl,
    TeamMissionRepoImpl,
)


REPOS_ROOT = Path(__file__).parent.parent.parent / "hermes_agent" / "repositories"


# Aggregate root → its owned tables (spec §4).
_AGGREGATE_TABLES = {
    "session": {
        "sessions",
        "session_index",
        "session_branches",
        "session_handoffs",
        "session_lineage",
        "session_branch_requests",
    },
    "run": {
        "runs",
        "run_events",
    },
    "message": {
        "messages",
        "messages_fts",  # FTS virtual table alias
    },
    "team_mission": {
        "team_missions",
        "team_mission_nodes",
        "team_mission_edges",
        "team_mission_run_bindings",
        "team_mission_conversations",
        "team_mission_events",  # legacy; may still be referenced in migration paths
        "activities",
        "activities_legacy_kind_check",
        "activity_commands",
        "v3_activities",
    },
    "agent_profile": {
        "agent_profiles",
        "agent_profile_versions",
        "agent_profile_growth_summary",
        "agent_profile_drafts",
    },
}


# System-owned tables that L2 domain services legitimately use.
_DOMAIN_SHARED_TABLES = {
    "seq_counter",  # SeqAllocator; RepoImpls call SeqAllocator.allocate_only(...)
    "sqlite_master",  # SQLite schema introspection for owner-managed migrations.
}


_AGGREGATE_FILES = {
    "session": "session_repo.py",
    "run": "run_repo.py",
    "message": "message_repo.py",
    "team_mission": "team_mission_repo.py",
    "agent_profile": "agent_profile_repo.py",
}


# SQLite table-name references in raw SQL text. Matches `FROM foo`, `JOIN
# foo`, `INTO foo`, `UPDATE foo`, `TABLE foo` (case-insensitive).
_TABLE_REF_RE = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE|DELETE\s+FROM)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
    re.IGNORECASE,
)


def _scan_table_references(path: Path) -> set[str]:
    """Extract every distinct SQL table name referenced in ``path``.

    Restrict the scan to string literals inside the module — prose in
    module/class/function docstrings and Python identifiers never look
    like SQL, so ignore everything except non-docstring string literals.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    refs: set[str] = set()

    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_ids.add(id(body[0].value))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstring_ids:
                continue
            for match in _TABLE_REF_RE.finditer(node.value):
                table = match.group(1).lower()
                if table in {"select", "where", "if", "not", "exists", "set"}:
                    continue
                refs.add(table)
    return refs


def _allowed_for(aggregate: str) -> set[str]:
    return _AGGREGATE_TABLES[aggregate] | _DOMAIN_SHARED_TABLES


# ---------------------------------------------------------------------------


def test_session_repo_only_touches_session_tables():
    refs = _scan_table_references(REPOS_ROOT / _AGGREGATE_FILES["session"])
    allowed = _allowed_for("session")
    leaks = refs - allowed
    assert not leaks, (
        f"SessionRepoImpl touches non-session tables: {sorted(leaks)!r}"
    )


def test_run_repo_only_touches_run_tables():
    refs = _scan_table_references(REPOS_ROOT / _AGGREGATE_FILES["run"])
    allowed = _allowed_for("run")
    leaks = refs - allowed
    assert not leaks, (
        f"RunRepoImpl touches non-run tables: {sorted(leaks)!r}"
    )


def test_message_repo_only_touches_message_tables():
    refs = _scan_table_references(REPOS_ROOT / _AGGREGATE_FILES["message"])
    allowed = _allowed_for("message")
    leaks = refs - allowed
    assert not leaks, (
        f"MessageRepoImpl touches non-message tables: {sorted(leaks)!r}"
    )


def test_team_mission_repo_only_touches_mission_tables():
    refs = _scan_table_references(REPOS_ROOT / _AGGREGATE_FILES["team_mission"])
    allowed = _allowed_for("team_mission")
    leaks = refs - allowed
    assert not leaks, (
        f"TeamMissionRepoImpl touches non-mission tables: {sorted(leaks)!r}"
    )


def test_agent_profile_repo_only_touches_profile_tables():
    refs = _scan_table_references(REPOS_ROOT / _AGGREGATE_FILES["agent_profile"])
    allowed = _allowed_for("agent_profile")
    leaks = refs - allowed
    assert not leaks, (
        f"AgentProfileRepoImpl touches non-profile tables: {sorted(leaks)!r}"
    )


def test_impl_classes_are_registered_in_repositories_init():
    """Meta — every RepoImpl class is exported so callers can consume the
    typed interface without deep imports.
    """
    for cls in (
        SessionRepoImpl,
        RunRepoImpl,
        MessageRepoImpl,
        TeamMissionRepoImpl,
        AgentProfileRepoImpl,
    ):
        assert cls.__module__.startswith("hermes_agent.repositories"), (
            f"{cls.__name__} lives outside hermes_agent.repositories package: "
            f"{cls.__module__}"
        )
