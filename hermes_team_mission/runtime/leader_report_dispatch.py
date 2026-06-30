from __future__ import annotations

from collections.abc import Callable
from typing import Any


LeaderReportSubmitter = Callable[..., dict[str, Any]]

_leader_report_submitter: LeaderReportSubmitter | None = None


def register_leader_report_submitter(submitter: LeaderReportSubmitter | None) -> None:
    global _leader_report_submitter
    _leader_report_submitter = submitter


def submit_leader_report_run(**kwargs: Any) -> dict[str, Any]:
    submitter = _leader_report_submitter
    if submitter is None:
        try:
            from hermes_team_mission.gateway import runtime_methods

            submitter = getattr(runtime_methods, "submit_mission_leader_report_run", None)
        except Exception:
            submitter = None
    if not callable(submitter):
        return {
            "ok": False,
            "status": "unavailable",
            "error": "leader_report_submitter_unavailable",
        }
    return submitter(**kwargs)
