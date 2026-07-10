"""ADR-0001 Phase 1.D bridge for legacy Activity Command audit rows.

This module is a stop-gap. After Phase 1.E + Phase 2 + Phase 3 migrate
the actual lifecycle to reconciler-driven commands, this bridge module
can be deleted; legacy RPCs will be replaced by Activity Commands
end-to-end.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from hermes_team_mission.domain.activity import ACTIVITY_COMMAND_KINDS

logger = logging.getLogger(__name__)


def record_legacy_activity_command(
    db: Any,
    *,
    activity_id: str,
    kind: str,
    payload: dict[str, Any] | None = None,
    source: str,
) -> str:
    """Best-effort insert of an activity_command row for audit.

    Returns the command_id on success, or "" on any failure. Legacy callers
    must keep executing when this bridge returns "".
    """
    activity_id = str(activity_id or "").strip()
    kind = str(kind or "").strip()
    if not activity_id or kind not in ACTIVITY_COMMAND_KINDS:
        return ""
    command_id = f"legacy-{kind}-{uuid.uuid4().hex}"
    try:
        db.activities.insert_command(
            command_id=command_id,
            activity_id=activity_id,
            kind=kind,
            payload=dict(payload or {}),
            metadata={"source": source, "via": "legacy_bridge"},
        )
        return command_id
    except Exception as exc:
        logger.debug(
            "record_legacy_activity_command failed source=%s activity=%s kind=%s: %s",
            source,
            activity_id,
            kind,
            exc,
        )
        return ""
