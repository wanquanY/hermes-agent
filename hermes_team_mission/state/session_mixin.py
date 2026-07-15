from __future__ import annotations

from .conversation_missions import TeamMissionConversationMissionMixin
from .session_context import TeamMissionContextMixin
from .session_conversations import TeamMissionConversationMixin
from .session_events import TeamMissionEventMixin
from .session_finalizers import TeamMissionFinalizerMixin
from .session_graph import TeamMissionGraphMixin


class TeamMissionStateMixin(
    TeamMissionConversationMissionMixin,
    TeamMissionConversationMixin,
    TeamMissionGraphMixin,
    TeamMissionEventMixin,
    TeamMissionContextMixin,
    TeamMissionFinalizerMixin,
):
    """Native Hermes Team Mission graph and run-binding persistence.

    Team Mission stores only mission-specific graph and ownership metadata here.
    Runtime facts remain in ordinary Hermes ``runs``, ``run_events``, and
    ``messages`` tables so stream coalescing, retention, and history semantics
    stay identical to ordinary sessions.
    """
