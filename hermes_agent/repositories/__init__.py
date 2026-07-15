"""L1 Repository layer (spec §4).

Five aggregate roots. Each ``Repo`` protocol is the sole gateway into its
owned tables. Concrete implementations land in Phase D2-D5 (spec §12); this
package exposes only the typed interface.

Cross-repository joins are forbidden — application services compose repositories.
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
    MessageRepository,
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
    BranchLineageSpec,
    BranchRequestSpec,
    BranchSpec,
    MaterializedBranchSessionSpec,
    Session,
    SessionFilter,
    SessionIndexPatch,
    SessionMessageAppendProjection,
    SessionMessageSnapshotProjection,
    SessionNotFound,
    SessionRepo,
    SessionRepoImpl,
    SessionRunProjection,
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
    RunConversationBinding,
    TeamMissionRepo,
    TeamMissionRepoImpl,
)

__all__ = [
    "ActivityKind",
    "AgentProfileRepo",
    "AgentProfileRepoImpl",
    "BranchLineageSpec",
    "BranchRequestSpec",
    "BranchSpec",
    "CanonicalEventSpec",
    "GrowthSummary",
    "Message",
    "MessagePage",
    "MessageRepository",
    "MessageRepo",
    "MessageRepoImpl",
    "MessageSpec",
    "MaterializedBranchSessionSpec",
    "PageDirection",
    "Profile",
    "ProfileSpec",
    "ProfileVersion",
    "ensure_agent_profile_repository_schema",
    "RepositoryConnection",
    "RepositoryContext",
    "Run",
    "RunConversationBinding",
    "RunRepo",
    "RunRepoImpl",
    "RunSpec",
    "Session",
    "SessionFilter",
    "SessionIndexPatch",
    "SessionMessageAppendProjection",
    "SessionMessageSnapshotProjection",
    "SessionNotFound",
    "SessionRepo",
    "SessionRepoImpl",
    "SessionRunProjection",
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
