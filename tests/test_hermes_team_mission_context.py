from __future__ import annotations

import hermes_team_mission.context.worker_context as context_module
from hermes_team_mission.context.worker_context import PRIOR_ATTEMPT_MAX_CHARS
from hermes_team_mission.context.worker_context import RECENT_EVENT_MAX_CHARS
from hermes_team_mission.context.worker_context import WORKER_CONTEXT_MAX_CHARS
from hermes_team_mission.context.worker_context import build_team_mission_worker_context
from hermes_team_mission.context.worker_context import prior_node_attempts
from hermes_team_mission.context.worker_context import recent_node_events
from hermes_team_mission.context.worker_context import team_mission_graph_summary
from hermes_state import SessionDB


def test_team_mission_worker_context_is_bounded_and_keeps_refs():
    mission = {
        "mission_id": "mission-1",
        "title": "Large context mission",
        "objective": "x" * 50_000,
        "status": "running",
    }
    node = {
        "node_id": "node-worker",
        "kind": "worker",
        "title": "Worker",
        "objective": "Do bounded work",
        "status": "ready",
        "metadata": {
            "task_brief": {
                "background": "b" * 50_000,
                "execution": ["e" * 5000 for _ in range(20)],
                "goal": "g" * 20_000,
                "acceptance_criteria": ["a" * 5000 for _ in range(20)],
                "inputs": ["/tmp/source.pdf", "artifact:123"],
            },
        },
        "output_contract": {"format": "structured", "notes": "o" * 20_000},
    }
    graph = {"mission": mission, "nodes": [node], "edges": []}

    context = build_team_mission_worker_context(
        mission=mission,
        node=node,
        graph=graph,
        memory_context={"kind": "worker_memory_slice", "item_ids": ["memory-1"]},
        memory_text="m" * 80_000,
    )

    assert context["kind"] == "team_mission_worker_context"
    assert context["text_chars"] <= WORKER_CONTEXT_MAX_CHARS
    assert context["task_brief"]["inputs"] == ["/tmp/source.pdf", "artifact:123"]
    assert context["memory"]["item_ids"] == ["memory-1"]
    assert "Acceptance criteria:" in context["text"]


def test_worker_context_budget_keeps_handoff_protocol_before_optional_sections(monkeypatch):
    monkeypatch.setattr(context_module, "WORKER_CONTEXT_MAX_CHARS", 24 * 1024)
    mission = {
        "mission_id": "mission-1",
        "title": "Large handoff mission",
        "objective": "x" * 50_000,
        "status": "running",
    }
    node = {
        "node_id": "node-worker",
        "kind": "worker",
        "title": "Worker",
        "objective": "Do bounded work",
        "status": "ready",
        "metadata": {
            "task_brief": {
                "background": "b" * 50_000,
                "execution": ["e" * 5000 for _ in range(20)],
                "goal": "g" * 20_000,
                "acceptance_criteria": ["a" * 5000 for _ in range(20)],
                "inputs": ["i" * 5000 for _ in range(20)],
                "deliverables": ["d" * 5000 for _ in range(20)],
                "constraints": ["c" * 5000 for _ in range(20)],
            },
        },
        "output_contract": {
            "format": "structured_deliverable",
            "delivery_channel": "handoff",
            "requires_explicit_handoff": True,
            "notes": "o" * 20_000,
        },
    }
    graph = {
        "mission": mission,
        "nodes": [
            {
                "node_id": "node-upstream",
                "kind": "worker",
                "title": "Upstream",
                "status": "completed",
                "deliverable": {
                    "status": "completed",
                    "result": "PASS",
                    "summary": "s" * 50_000,
                    "artifact_refs": [{"path": f"/tmp/artifact-{index}.md"} for index in range(20)],
                    "source": "authoritative",
                },
            },
            node,
        ] + [
            {
                "node_id": f"node-{index}",
                "kind": "worker",
                "title": "Optional graph node " + ("x" * 200),
                "objective": "Optional objective " + ("y" * 2000),
                "status": "ready",
            }
            for index in range(60)
        ],
        "edges": [{"from_node_id": "node-upstream", "to_node_id": "node-worker"}],
    }

    context = build_team_mission_worker_context(
        mission=mission,
        node=node,
        graph=graph,
        memory_text="m" * 80_000,
    )

    assert context["text_chars"] <= context_module.WORKER_CONTEXT_MAX_CHARS
    assert context["truncated"] is True
    assert "Handoff protocol:" in context["text"]
    assert "call team_mission_submit_deliverable" in context["text"]
    assert "Acceptance criteria:" in context["text"]
    assert "graph_summary" in context["dropped_sections"] or "team_memory" in context["dropped_sections"]


def test_team_mission_graph_summary_never_returns_full_node_payload():
    graph = {
        "mission": {"mission_id": "mission-1", "title": "Mission", "objective": "Objective", "status": "planning"},
        "nodes": [
            {
                "node_id": "node-worker",
                "kind": "worker",
                "title": "Worker",
                "objective": "Objective",
                "status": "ready",
                "metadata": {"task_brief": {"background": "b" * 5000, "goal": "Goal"}},
            }
        ],
        "edges": [],
    }

    summary = team_mission_graph_summary(graph)

    assert summary["node_count"] == 1
    assert summary["nodes"][0]["node_id"] == "node-worker"
    assert "task_brief" not in summary["nodes"][0]


def test_worker_context_includes_upstream_handoff_deliverables_and_protocol():
    mission = {
        "mission_id": "mission-1",
        "title": "Mission",
        "objective": "Use upstream structured context",
        "status": "running",
    }
    upstream = {
        "node_id": "node-research",
        "kind": "worker",
        "title": "Research",
        "status": "completed",
        "deliverable": {
            "status": "completed",
            "result": "PASS",
            "summary": "Research handoff summary.",
            "payload": {"finding": "Use the native handoff channel."},
            "artifact_refs": [{"path": "/tmp/research.md"}],
            "source": "authoritative",
        },
    }
    current = {
        "node_id": "node-build",
        "kind": "worker",
        "title": "Build",
        "objective": "Build from research.",
        "status": "ready",
        "output_contract": {
            "format": "structured_deliverable",
            "delivery_channel": "handoff",
            "requires_explicit_handoff": True,
        },
    }
    graph = {
        "mission": mission,
        "nodes": [upstream, current],
        "edges": [{"from_node_id": "node-research", "to_node_id": "node-build"}],
    }

    context = build_team_mission_worker_context(
        mission=mission,
        node=current,
        graph=graph,
    )

    assert "Upstream handoff deliverables" in context["text"]
    assert "Research handoff summary." in context["text"]
    assert "Use the native handoff channel." not in context["text"]
    assert "call team_mission_submit_deliverable" in context["text"]
    assert "Do not print the structured JSON deliverable" in context["text"]


def test_worker_context_includes_prior_attempts_and_recent_events(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.upsert_team_mission(
        mission_id="mission-1",
        team_id="team-1",
        title="Retry mission",
        objective="Retry one node with context.",
        status="running",
        mode="supervised_mission",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        objective="Complete the worker task.",
        status="running",
        output_contract={"format": "structured_deliverable"},
    )
    db.upsert_run(run_id="run-old", session_id="session-old", status="running")
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-old",
        session_id="session-old",
        runtime_scope_key="team:mission-1:node:node-worker",
        role="worker",
    )
    db.upsert_team_mission_deliverable(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-old",
        status="partial",
        result="PARTIAL",
        summary="First attempt wrote the draft but missed validation.",
        payload={"status": "partial"},
    )
    db.append_team_mission_run_event(
        mission_id="mission-1",
        run_id="run-old",
        event={
            "type": "message.complete",
            "seq": 1,
            "payload": {
                "status": "error",
                "error": "worker exited before validation finished",
            },
        },
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        objective="Complete the worker task.",
        status="ready",
        output_contract={"format": "structured_deliverable"},
    )
    graph = db.get_team_mission_graph("mission-1")
    node = next(item for item in graph["nodes"] if item["node_id"] == "node-worker")

    context = build_team_mission_worker_context(
        mission=graph["mission"],
        node=node,
        graph=graph,
        db=db,
    )

    assert context["text_chars"] <= WORKER_CONTEXT_MAX_CHARS
    assert "Prior attempts on this node:" in context["text"]
    assert "First attempt wrote the draft but missed validation." in context["text"]
    assert "Recent events:" in context["text"]
    assert "worker exited before validation finished" in context["text"]


def test_worker_context_prior_attempts_and_recent_events_have_per_item_caps(monkeypatch, tmp_path):
    db = SessionDB(tmp_path / "state.db")
    long_attempt_summary = "attempt-" + ("a" * (PRIOR_ATTEMPT_MAX_CHARS + 500))
    db.upsert_team_mission(
        mission_id="mission-1",
        team_id="team-1",
        title="Retry mission",
        objective="Retry with bounded item payloads.",
        status="running",
        mode="supervised_mission",
    )
    db.upsert_team_mission_node(
        mission_id="mission-1",
        node_id="node-worker",
        kind="worker",
        title="Worker",
        objective="Complete the worker task.",
        status="running",
    )
    db.upsert_run(run_id="run-old", session_id="session-old", status="running")
    db.bind_team_mission_run(
        mission_id="mission-1",
        node_id="node-worker",
        run_id="run-old",
        session_id="session-old",
        runtime_scope_key="team:mission-1:node:node-worker",
        role="worker",
    )
    monkeypatch.setattr(
        db,
        "latest_team_mission_deliverable_for_run",
        lambda _run_id: {"status": "partial", "summary": long_attempt_summary},
    )
    monkeypatch.setattr(
        db,
        "list_team_mission_events",
        lambda _mission_id, limit=500: [
            {
                "seq": 1,
                "type": "message.complete",
                "payload": {
                    "kind": "node.failed",
                    "node_id": "node-worker",
                    "source_event_type": "message.complete",
                    "source_payload": {"status": "error", "text": long_attempt_summary},
                },
            }
        ],
    )

    prior = prior_node_attempts(db, "mission-1", "node-worker")
    recent = recent_node_events(db, "mission-1", "node-worker")

    assert len(prior) == 1
    assert len(prior[0]["summary"]) <= PRIOR_ATTEMPT_MAX_CHARS
    assert prior[0]["summary"].endswith(" ... [truncated]")
    assert len(recent) == 1
    assert len(recent[0]["summary"]) <= RECENT_EVENT_MAX_CHARS
    assert recent[0]["summary"].endswith(" ... [truncated]")
