"""Team Mission state maintenance routines."""

from __future__ import annotations

from typing import Any

from hermes_team_mission.state.conversation import repair_legacy_team_mission_conversation_sessions
from hermes_team_mission.state.conversation import repair_placeholder_team_mission_conversation_titles


def run_team_mission_startup_maintenance(db: Any, logger: Any) -> None:
    """Run Team Mission repair tasks after base schema initialization."""

    try:
        repaired = repair_legacy_team_mission_conversation_sessions(db)
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
        retitled = repair_placeholder_team_mission_conversation_titles(db)
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
        repaired_running = db.session_index.repair_terminal_active_runs()
        if repaired_running:
            logger.info(
                "repaired %d stale Team Mission running session index row(s)",
                repaired_running,
            )
    except Exception as repair_running_exc:
        logger.warning(
            "stale Team Mission running session index repair skipped: %s",
            repair_running_exc,
        )
