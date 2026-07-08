"""L1 Repository layer (spec §4).

Five aggregate roots. Each ``Repo`` protocol is the sole gateway into its
owned tables. Concrete implementations land in Phase D2-D5 (spec §12); this
package exposes only the typed interface.

Cross-repository joins are forbidden — L2 domain services compose repositories.
"""

from __future__ import annotations

from hermes_agent.repositories.agent_profile_repo import (
    AgentProfileRepo,
    AgentProfileRepoImpl,
    GrowthSummary,
    Profile,
    ProfileSpec,
    ProfileVersion,
    ensure_agent_profile_repository_schema,
)
from hermes_agent.repositories.base import RepositoryConnection, RepositoryContext
from hermes_agent.repositories.message_repo import (
    Message,
    MessagePage,
    MessageRepo,
    MessageRepoImpl,
    MessageSpec,
    PageDirection,
)
from hermes_agent.repositories.run_repo import (
    CanonicalEventSpec,
    Run,
    RunRepo,
    RunRepoImpl,
    RunSpec,
)
from hermes_agent.repositories.session_repo import (
    BranchSpec,
    Session,
    SessionFilter,
    SessionIndexPatch,
    SessionNotFound,
    SessionRepo,
    SessionRepoImpl,
    SessionSpec,
)
from hermes_agent.repositories.team_mission_repo import (
    Activity,
    ActivityKind,
    ActivitySpec,
    ActivityStatus,
    EdgeSpec,
    Mission,
    MissionEdge,
    MissionGraph,
    MissionNode,
    MissionSpec,
    NodeSpec,
    NodeStatus,
    TeamMissionRepo,
    TeamMissionRepoImpl,
)

__all__ = [
    "ActivityKind",
    "AgentProfileRepo",
    "AgentProfileRepoImpl",
    "BranchSpec",
    "CanonicalEventSpec",
    "GrowthSummary",
    "Message",
    "MessagePage",
    "MessageRepo",
    "MessageRepoImpl",
    "MessageSpec",
    "PageDirection",
    "Profile",
    "ProfileSpec",
    "ProfileVersion",
    "ensure_agent_profile_repository_schema",
    "RepositoryConnection",
    "RepositoryContext",
    "Run",
    "RunRepo",
    "RunRepoImpl",
    "RunSpec",
    "Session",
    "SessionFilter",
    "SessionIndexPatch",
    "SessionNotFound",
    "SessionRepo",
    "SessionRepoImpl",
    "SessionSpec",
    "Activity",
    "ActivitySpec",
    "ActivityStatus",
    "EdgeSpec",
    "Mission",
    "MissionEdge",
    "MissionGraph",
    "MissionNode",
    "MissionSpec",
    "NodeSpec",
    "NodeStatus",
    "TeamMissionRepo",
    "TeamMissionRepoImpl",
]
