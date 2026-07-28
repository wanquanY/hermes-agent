"""Canonical Participant/Activity ownership for team-conversation runs.

Visible runtime events may only reference entities that already exist in the
conversation journal.  This module owns the persist-before-publish boundary so
member-chat dispatch cannot race its Participant or Activity into existence.
"""

from __future__ import annotations

from typing import Any

from hermes_team_mission.read_models.conversation_activity_projection import (
    project_team_mission_activities,
)
from tui_gateway.services import run_control


PARTICIPANT_UPSERTED_EVENT = "participant.upserted"
ACTIVITY_UPSERTED_EVENT = "activity.upserted"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _publish_owner_entity(
    db: Any,
    *,
    conversation_session_id: str,
    event_type: str,
    participant_id: str,
    runtime_scope_key: str,
    payload: dict[str, Any],
    activity_id: str = "",
) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "type": event_type,
        "conversation_session_id": conversation_session_id,
        "session_id": conversation_session_id,
        "participant_id": participant_id,
        "runtime_scope_key": runtime_scope_key,
        "payload": payload,
    }
    if activity_id:
        frame["activity_id"] = activity_id
        frame["activityId"] = activity_id
    run_control.publish_recorded_event(frame, db=db, persist=True)
    if frame.get("transient") is True or int(frame.get("seq") or 0) <= 0:
        raise RuntimeError(f"{event_type} was not durably journaled")
    return frame


def ensure_member_chat_owner_entities(
    db: Any,
    *,
    conversation_session_id: str,
    participant_id: str,
    member_id: str,
    agent_profile_id: str,
    agent_profile_version_id: str,
    runtime_scope_key: str,
    display_name: str,
    avatar: str,
    prompt_summary: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Materialize and publish the causal owners for one member-chat run.

    Re-publishing the current entity state is intentional. A subscriber may
    have connected before an entity was created, while upsert events remain
    idempotent for reconnect and retry.
    """
    stable_session_id = _text(conversation_session_id)
    stable_participant_id = _text(participant_id)
    stable_member_id = _text(member_id)
    stable_scope = _text(runtime_scope_key)
    if not stable_session_id or not stable_participant_id or not stable_member_id:
        raise ValueError("member-chat owner identity is incomplete")
    if not stable_scope:
        raise ValueError("member-chat runtime_scope_key is required")

    participant = db.participants.upsert_conversation_participant(
        conversation_session_id=stable_session_id,
        participant_id=stable_participant_id,
        role="member",
        member_id=stable_member_id,
        agent_profile_id=_text(agent_profile_id),
        agent_profile_version_id=_text(agent_profile_version_id),
        runtime_scope_key=stable_scope,
        display_name=_text(display_name),
        avatar=_text(avatar),
    )
    if not isinstance(participant, dict) or not participant:
        raise RuntimeError("member-chat Participant was not persisted")

    activity_id = f"act-member_chat:{stable_session_id}:{stable_member_id}"
    activity = db.activities.get(activity_id)
    if not isinstance(activity, dict) or not activity:
        activity = db.activities.create(
            activity_id=activity_id,
            conversation_id=stable_session_id,
            kind="member_chat",
            target_profile_id=_text(agent_profile_id) or None,
            status="running",
            prompt_summary=_text(prompt_summary)[:500] or None,
            notify_parent=False,
        )
    if not isinstance(activity, dict) or not activity:
        raise RuntimeError("member-chat Activity was not persisted")
    if _text(activity.get("conversation_id")) != stable_session_id:
        raise RuntimeError("member-chat Activity belongs to another conversation")
    if _text(activity.get("kind")) != "member_chat":
        raise RuntimeError("member-chat Activity has an incompatible kind")

    _publish_owner_entity(
        db,
        conversation_session_id=stable_session_id,
        event_type=PARTICIPANT_UPSERTED_EVENT,
        participant_id=stable_participant_id,
        runtime_scope_key=stable_scope,
        payload={"participant": participant},
    )
    _publish_owner_entity(
        db,
        conversation_session_id=stable_session_id,
        event_type=ACTIVITY_UPSERTED_EVENT,
        participant_id=stable_participant_id,
        runtime_scope_key=stable_scope,
        activity_id=activity_id,
        payload={"activity": activity},
    )
    return participant, activity


def publish_team_mission_activity_entities(
    db: Any,
    *,
    mission_id: str,
) -> list[dict[str, Any]]:
    """Publish the canonical Mission/Node Activities to the visible journal.

    Graph rows are persisted before this boundary.  Emitting the whole current
    projection is intentional: ``activity.upserted`` is idempotent, which makes
    retries and reconnect races converge without a second client-side graph
    store.
    """

    activities = project_team_mission_activities(db, mission_id)
    for activity in activities:
        conversation_session_id = _text(activity.get("conversation_id"))
        activity_id = _text(activity.get("activity_id"))
        participant_id = _text(activity.get("owner_participant_id"))
        if not conversation_session_id or not activity_id:
            raise RuntimeError(
                "team mission Activity projection identity is incomplete"
            )
        try:
            prior_events = db.runs.list_events_by_activity(
                activity_id,
                limit=2000,
                include_internal=True,
            )
        except Exception:
            prior_events = []
        latest_upsert = next(
            (
                event
                for event in reversed(prior_events)
                if _text(
                    event.get("conversation_session_id") or event.get("session_id")
                )
                == conversation_session_id
                and _text(event.get("type")) == ACTIVITY_UPSERTED_EVENT
            ),
            None,
        )
        if (
            isinstance(latest_upsert, dict)
            and isinstance(latest_upsert.get("payload"), dict)
            and latest_upsert["payload"].get("activity") == activity
        ):
            continue
        runtime_scope_key = f"activity:{activity_id}"
        if participant_id:
            try:
                participant = db.participants.get_participant(
                    conversation_session_id,
                    participant_id,
                )
            except Exception:
                participant = None
            if isinstance(participant, dict):
                runtime_scope_key = (
                    _text(participant.get("runtime_scope_key")) or runtime_scope_key
                )
        _publish_owner_entity(
            db,
            conversation_session_id=conversation_session_id,
            event_type=ACTIVITY_UPSERTED_EVENT,
            participant_id=participant_id,
            runtime_scope_key=runtime_scope_key,
            activity_id=activity_id,
            payload={"activity": activity},
        )
    return activities


__all__ = [
    "ACTIVITY_UPSERTED_EVENT",
    "PARTICIPANT_UPSERTED_EVENT",
    "ensure_member_chat_owner_entities",
    "publish_team_mission_activity_entities",
]
