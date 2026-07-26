"""Compatibility imports for the domain-owned message owner projection."""

from hermes_agent.domain.message_owner_projection import (
    MessageOwnerResolutionError,
    message_participant_id,
    participant_id_for_message_from_events,
    project_render_message_owners,
    run_event_participant_index,
    with_message_participant_id,
)

__all__ = [
    "MessageOwnerResolutionError",
    "message_participant_id",
    "participant_id_for_message_from_events",
    "project_render_message_owners",
    "run_event_participant_index",
    "with_message_participant_id",
]
