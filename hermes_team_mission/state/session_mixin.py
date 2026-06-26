from __future__ import annotations

from .session_context import SessionDBTeamMissionContextMixin
from .session_conversations import SessionDBTeamMissionConversationMixin
from .session_events import SessionDBTeamMissionEventMixin
from .session_finalizers import SessionDBTeamMissionFinalizerMixin
from .session_graph import SessionDBTeamMissionGraphMixin
from .session_rows import SessionDBTeamMissionRowsMixin
from .session_views import SessionDBTeamMissionViewMixin


class SessionDBTeamMissionMixin(
    SessionDBTeamMissionRowsMixin,
    SessionDBTeamMissionConversationMixin,
    SessionDBTeamMissionGraphMixin,
    SessionDBTeamMissionEventMixin,
    SessionDBTeamMissionViewMixin,
    SessionDBTeamMissionContextMixin,
    SessionDBTeamMissionFinalizerMixin,
):
    """Native Hermes Team Mission graph and run-binding persistence.

    Team Mission stores only mission-specific graph and ownership metadata here.
    Runtime facts remain in ordinary Hermes ``runs``, ``run_events``, and
    ``messages`` tables so stream coalescing, retention, and history semantics
    stay identical to ordinary sessions.
    """
