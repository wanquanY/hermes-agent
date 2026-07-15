"""Phase D1 — Repository Protocol骨架就绪 (spec §4)."""

from __future__ import annotations

from typing import Protocol, get_type_hints

import pytest

from hermes_agent.repositories import (
    ActivityKind,
    AgentProfileRepo,
    MessageRepo,
    RepositoryConnection,
    RepositoryContext,
    RunRepo,
    SessionRepo,
    TeamMissionRepo,
)


_ALL_REPOS = [SessionRepo, RunRepo, MessageRepo, TeamMissionRepo, AgentProfileRepo]


@pytest.mark.parametrize("repo", _ALL_REPOS)
def test_repo_is_protocol(repo):
    """Each repo is a typing.Protocol (structural typing anchor for D2-D5)."""
    assert issubclass(repo, Protocol), f"{repo.__name__} must inherit Protocol"


@pytest.mark.parametrize(
    "repo,expected_methods",
    [
        (SessionRepo, {"create", "get", "list", "update_index", "branch", "close"}),
        (
            RunRepo,
            {
                "create_run",
                "get_run",
                "append_event",
                "list_events",
                "list_tool_events",
                "set_terminal",
            },
        ),
        (
            MessageRepo,
            {"append", "get_page", "search_fts", "replace_all", "merge_metadata"},
        ),
        (
            TeamMissionRepo,
            {
                "create_mission",
                "get_graph",
                "get_node",
                "bind_run",
                "update_node_status",
                "append_activity",
                "list_activities",
                "update_activity_status",
            },
        ),
        (
            AgentProfileRepo,
            {"create_profile", "get", "add_version", "get_growth_summary"},
        ),
    ],
)
def test_repo_declares_spec_methods(repo, expected_methods):
    """spec §4 method contract per aggregate root."""
    declared = {
        name
        for name in vars(repo)
        if not name.startswith("_") and callable(vars(repo)[name])
    }
    missing = expected_methods - declared
    assert not missing, f"{repo.__name__} missing spec methods: {missing}"


def test_team_mission_repo_owns_activities():
    """spec §4.4 P0-B4 — TeamMissionRepo, not a separate ActivityRepo."""
    assert hasattr(TeamMissionRepo, "append_activity"), (
        "activities table ownership moved to TeamMissionRepo (v3.0.2 P0-B4)"
    )
    assert hasattr(TeamMissionRepo, "list_activities")
    assert hasattr(TeamMissionRepo, "update_activity_status")


def test_activity_kind_literal_covers_4_kinds():
    """spec §4.4 — 4 activity kinds shared with frontend items SSoT."""
    # ActivityKind is a Literal; introspect the args.
    from typing import get_args

    kinds = set(get_args(ActivityKind))
    assert kinds == {
        "async_agent_dispatch",
        "async_team_dispatch",
        "team_mission_activity",
        "dispatch_completion",
    }


def test_repository_context_carries_conn_and_owner():
    """base.RepositoryContext is the DI vehicle for D2-D5."""
    import sqlite3

    conn = sqlite3.connect(":memory:")
    ctx = RepositoryContext(conn=conn, owner="legacy-facade")
    assert ctx.conn is conn
    assert ctx.owner == "legacy-facade"
    conn.close()


def test_run_repo_list_events_has_include_internal_flag():
    """spec §7.4 — list_events default excludes _internal.* interaction events."""
    hints = get_type_hints(RunRepo.list_events)
    assert "include_internal" in RunRepo.list_events.__code__.co_varnames
