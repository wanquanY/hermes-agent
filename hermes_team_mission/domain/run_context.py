"""Conversation Runtime Protocol P2 run context data structure.

RunContext replaces the loose stored_session_id / runtime_scope_key /
agent_profile_id variables currently spread across worker-spawn parameters.
This PR only carries the structure through spawn payloads; runtime event
routing consumes it in a later Conversation Runtime Protocol phase.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from hermes_team_mission.domain.activity import ACTIVITY_ID_FORMAT_PATTERN


_ACTIVITY_KINDS = frozenset({"chat", "member_chat", "mission"})


@dataclass(frozen=True)
class RunContext:
    conversation_session_id: str
    participant_id: str
    activity_id: str
    activity_kind: str
    execution_scope_key: str
    control_home: str
    execution_home: str

    def __post_init__(self) -> None:
        for field_name in (
            "conversation_session_id",
            "participant_id",
            "activity_id",
            "activity_kind",
            "execution_scope_key",
            "control_home",
            "execution_home",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise ValueError(f"{field_name} must be a string")
            if field_name != "activity_id" and not value.strip():
                raise ValueError(f"{field_name} is required and cannot be empty")

        if self.activity_kind not in _ACTIVITY_KINDS:
            allowed = ", ".join(sorted(_ACTIVITY_KINDS))
            raise ValueError(f"activity_kind must be one of: {allowed}")

        activity_id = str(self.activity_id or "").strip()
        if activity_id and not ACTIVITY_ID_FORMAT_PATTERN.match(activity_id):
            raise ValueError(
                f"RunContext.activity_id {activity_id!r} does not match the "
                "ADR-0001 format pattern "
                "'(mission|chat|team-conversation):.+ | act-<kind>:.+ | act-<kind>-.+'"
            )

        for field_name in ("control_home", "execution_home"):
            value = getattr(self, field_name)
            if not Path(value).expanduser().is_absolute():
                raise ValueError(f"{field_name} must be an absolute path")

    def to_payload(self) -> dict[str, str]:
        return {
            "conversation_session_id": self.conversation_session_id,
            "participant_id": self.participant_id,
            "activity_id": self.activity_id,
            "activity_kind": self.activity_kind,
            "execution_scope_key": self.execution_scope_key,
            "control_home": self.control_home,
            "execution_home": self.execution_home,
        }

    @classmethod
    def from_payload(cls, data: dict[str, Any] | str) -> RunContext:
        if isinstance(data, str):
            try:
                parsed = json.loads(data)
            except json.JSONDecodeError as exc:
                raise ValueError(f"run context payload is not valid JSON: {exc.msg}") from exc
        else:
            parsed = data

        if not isinstance(parsed, dict):
            raise ValueError("run context payload must be a dict or JSON object string")

        required = (
            "conversation_session_id",
            "participant_id",
            "activity_id",
            "activity_kind",
            "execution_scope_key",
            "control_home",
            "execution_home",
        )
        missing = [field_name for field_name in required if field_name not in parsed]
        if missing:
            raise ValueError(f"run context payload missing required fields: {', '.join(missing)}")

        return cls(**{field_name: parsed[field_name] for field_name in required})
