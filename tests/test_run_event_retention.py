from __future__ import annotations

from hermes_team_mission.runtime.run_event_retention import RunEventRetentionPolicy


def test_run_event_retention_policy_keeps_tool_complete_out_of_stream_pruning():
    policy = RunEventRetentionPolicy()

    decision = policy.classify({"type": "tool.complete"})

    assert decision.retention_class == "tool_fact"
    assert decision.prunable_after_terminal is False
    assert "tool.complete" not in policy.terminal_prunable_event_types()
