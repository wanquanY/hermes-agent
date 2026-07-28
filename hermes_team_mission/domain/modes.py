from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from hermes_team_mission.domain.node_kinds import metadata_with_normalized_node_kind
from hermes_team_mission.domain.node_kinds import normalize_team_mission_node_kind


MODE_DISCUSSION = "discussion"
MODE_SUPERVISED_MISSION = "supervised_mission"
MODE_AUTONOMOUS_MISSION = "autonomous_mission"
MODE_MANUAL_GRAPH = "manual_graph"

TEAM_MISSION_MODES = {
    MODE_DISCUSSION,
    MODE_SUPERVISED_MISSION,
    MODE_AUTONOMOUS_MISSION,
    MODE_MANUAL_GRAPH,
}


def _tuple_text(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").split(",")
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        raw_items = value
    else:
        raw_items = (value,)
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        normalized = str(item or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)


@dataclass(frozen=True)
class TeamMissionMember:
    member_id: str
    profile_id: str = ""
    profile_version_id: str = ""
    runtime_scope_key: str = ""
    hermes_home_path: str = ""
    dovie_profile: dict[str, Any] = field(default_factory=dict)
    display_name: str = ""
    role: str = "worker"
    status: str = "active"
    capability_tags: tuple[str, ...] = ()
    profile_summary: str = ""
    best_for_tasks: tuple[str, ...] = ()
    avoid_tasks: tuple[str, ...] = ()
    strengths: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    default_toolsets: tuple[str, ...] = ()
    recommended_skills: tuple[str, ...] = ()
    radar_scores: tuple[Mapping[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any] | "TeamMissionMember") -> "TeamMissionMember":
        if isinstance(raw, TeamMissionMember):
            return raw
        dovie_profile = dict(raw.get("dovie_profile") or raw.get("dovieProfile") or {})
        member_id = str(
            raw.get("member_id")
            or raw.get("memberId")
            or raw.get("id")
            or raw.get("profile_id")
            or raw.get("profileId")
            or raw.get("agent_profile_id")
            or raw.get("agentProfileId")
            or dovie_profile.get("memberId")
            or dovie_profile.get("member_id")
            or ""
        ).strip()
        profile_id = str(
            raw.get("profile_id")
            or raw.get("profileId")
            or raw.get("agent_profile_id")
            or raw.get("agentProfileId")
            or dovie_profile.get("id")
            or dovie_profile.get("agentProfileId")
            or dovie_profile.get("agent_profile_id")
            or member_id
        ).strip()
        return cls(
            member_id=member_id,
            profile_id=profile_id,
            profile_version_id=str(
                raw.get("profile_version_id")
                or raw.get("profileVersionId")
                or raw.get("agent_profile_version_id")
                or raw.get("agentProfileVersionId")
                or raw.get("version_id")
                or raw.get("versionId")
                or raw.get("current_version_id")
                or raw.get("currentVersionId")
                or dovie_profile.get("agentProfileVersionId")
                or dovie_profile.get("agent_profile_version_id")
                or dovie_profile.get("versionId")
                or dovie_profile.get("version_id")
                or ""
            ),
            runtime_scope_key=str(
                raw.get("runtime_scope_key")
                or raw.get("runtimeScopeKey")
                or dovie_profile.get("runtimeScopeKey")
                or dovie_profile.get("runtime_scope_key")
                or ""
            ),
            hermes_home_path=str(
                raw.get("hermes_home_path")
                or raw.get("hermesHomePath")
                or dovie_profile.get("hermesHomePath")
                or dovie_profile.get("hermes_home_path")
                or dovie_profile.get("hermes_home")
                or ""
            ),
            dovie_profile=dovie_profile,
            display_name=str(raw.get("display_name") or raw.get("displayName") or raw.get("name") or member_id),
            role=str(raw.get("role") or "worker"),
            status=str(raw.get("status") or "active"),
            capability_tags=_tuple_text(raw.get("capability_tags") or raw.get("capabilityTags")),
            profile_summary=str(raw.get("profile_summary") or raw.get("profileSummary") or ""),
            best_for_tasks=_tuple_text(raw.get("best_for_tasks") or raw.get("bestForTasks")),
            avoid_tasks=_tuple_text(raw.get("avoid_tasks") or raw.get("avoidTasks")),
            strengths=_tuple_text(raw.get("strengths")),
            limitations=_tuple_text(raw.get("limitations")),
            default_toolsets=_tuple_text(raw.get("default_toolsets") or raw.get("defaultToolsets")),
            recommended_skills=_tuple_text(raw.get("recommended_skills") or raw.get("recommendedSkills")),
            radar_scores=tuple(item for item in (raw.get("radar_scores") or raw.get("radarScores") or ()) if isinstance(item, Mapping)),
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass(frozen=True)
class TeamMissionNodeSpec:
    node_id: str
    kind: str
    title: str
    objective: str = ""
    status: str = "todo"
    assignee_profile_id: str = ""
    assignee_profile_version_id: str = ""
    runtime_scope_key: str = ""
    output_contract: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    position_x: float = 0
    position_y: float = 0


@dataclass(frozen=True)
class TeamMissionEdgeSpec:
    from_node_id: str
    to_node_id: str
    edge_id: str = ""
    kind: str = "depends_on"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TeamMissionGraphPatch:
    mission_status: str
    nodes: tuple[TeamMissionNodeSpec, ...] = ()
    edges: tuple[TeamMissionEdgeSpec, ...] = ()
    start_leader: bool = False
    auto_start_ready_nodes: bool = False
    requires_whole_graph_approval: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TeamMissionStrategyActions:
    mission_status: str = ""
    nodes: tuple[TeamMissionNodeSpec, ...] = ()
    edges: tuple[TeamMissionEdgeSpec, ...] = ()
    start_node_ids: tuple[str, ...] = ()
    approval_requests: tuple[dict[str, Any], ...] = ()
    events: tuple[dict[str, Any], ...] = ()
    auto_start_ready_nodes: bool = False


@dataclass(frozen=True)
class RiskDecision:
    action: str
    reason: str
    scope: str = "node"

    @property
    def allowed(self) -> bool:
        return self.action == "allow"


class TeamMissionModeStrategy:
    mode = ""

    def initialize_graph(
        self,
        *,
        mission_id: str,
        title: str,
        objective: str,
        members: Sequence[Mapping[str, Any] | TeamMissionMember] = (),
        graph_payload: Mapping[str, Any] | None = None,
    ) -> TeamMissionGraphPatch:
        raise NotImplementedError

    def should_start_leader(self) -> bool:
        return False

    def leader_start_text(
        self,
        *,
        mission_id: str,
        title: str,
        objective: str,
        members: Sequence[Mapping[str, Any] | TeamMissionMember] = (),
    ) -> str:
        del mission_id, title, members
        return str(objective or "").strip()

    def requires_whole_graph_approval(self) -> bool:
        return False

    def allow_automatic_graph_mutation(self, *, phase: str = "") -> bool:
        return False

    def on_plan_completed(
        self,
        *,
        mission_id: str,
        planned_nodes: Sequence[TeamMissionNodeSpec] = (),
        planned_edges: Sequence[TeamMissionEdgeSpec] = (),
    ) -> TeamMissionStrategyActions:
        return TeamMissionStrategyActions(
            mission_status="running",
            nodes=tuple(planned_nodes),
            edges=tuple(planned_edges),
            auto_start_ready_nodes=True,
        )

    def select_ready_nodes(self, graph: Mapping[str, Any]) -> tuple[str, ...]:
        nodes = graph.get("nodes") if isinstance(graph, Mapping) else []
        selected: list[str] = []
        for node in nodes if isinstance(nodes, Sequence) else []:
            if not isinstance(node, Mapping):
                continue
            if str(node.get("status") or "") != "ready":
                continue
            metadata = node.get("metadata") if isinstance(node.get("metadata"), Mapping) else {}
            if metadata.get("manual_start") is True:
                continue
            selected.append(str(node.get("node_id") or ""))
        return tuple(node_id for node_id in selected if node_id)

    def can_mutate_graph(self, *, actor: str, phase: str = "") -> bool:
        if actor == "user":
            return True
        return self.allow_automatic_graph_mutation(phase=phase)

    def risk_policy(self, *, risk_level: str = "low", action_kind: str = "") -> RiskDecision:
        if str(risk_level or "low") in {"high", "critical"}:
            return RiskDecision("require_approval", "high risk action requires approval")
        return RiskDecision("allow", "low risk action")

    def reduce_status(self, *, graph: Mapping[str, Any]) -> str:
        nodes = graph.get("nodes") if isinstance(graph, Mapping) else []
        statuses = {
            str(node.get("status") or "")
            for node in nodes if isinstance(node, Mapping)
        }
        if not statuses:
            return "draft"
        if statuses <= {"completed", "verified"}:
            return "completed"
        if "failed" in statuses:
            return "failed"
        if "blocked" in statuses and not (statuses & {"running", "ready"}):
            return "blocked"
        if "running" in statuses:
            return "running"
        if "ready" in statuses:
            return "ready"
        return "draft"

    def _root_node(
        self,
        *,
        mission_id: str,
        title: str,
        objective: str,
        status: str,
        phase: str,
        leader: TeamMissionMember | None,
    ) -> TeamMissionNodeSpec:
        return TeamMissionNodeSpec(
            node_id=_node_id(mission_id, "root"),
            kind="root",
            title=title or "Team mission",
            objective=objective,
            status=status,
            assignee_profile_id=leader.profile_id if leader else "",
            assignee_profile_version_id=leader.profile_version_id if leader else "",
            runtime_scope_key=f"team:{mission_id}:leader",
            output_contract={
                "format": "structured",
                "requires_process_events": True,
                "requires_deliverable": True,
            },
            metadata={
                "mode": self.mode,
                "phase": phase,
                "role": "leader",
            },
            position_x=0,
            position_y=0,
        )


class DiscussionStrategy(TeamMissionModeStrategy):
    mode = MODE_DISCUSSION

    def initialize_graph(
        self,
        *,
        mission_id: str,
        title: str,
        objective: str,
        members: Sequence[Mapping[str, Any] | TeamMissionMember] = (),
        graph_payload: Mapping[str, Any] | None = None,
    ) -> TeamMissionGraphPatch:
        normalized = _normalize_members(members)
        leader = _leader_member(normalized)
        participants = _worker_members(normalized)
        root = self._root_node(
            mission_id=mission_id,
            title=title,
            objective=objective,
            status="running",
            phase="discussion",
            leader=leader,
        )
        nodes: list[TeamMissionNodeSpec] = [root]
        edges: list[TeamMissionEdgeSpec] = []
        for index, member in enumerate(participants):
            node = TeamMissionNodeSpec(
                node_id=_node_id(mission_id, f"discussion-{member.member_id or index}"),
                kind="discussion",
                title=f"{member.display_name or member.member_id} 参与讨论",
                objective=objective,
                status="ready",
                assignee_profile_id=member.profile_id,
                assignee_profile_version_id=member.profile_version_id,
                runtime_scope_key=f"team:{mission_id}:discussion:{member.member_id or index}",
                output_contract={
                    "format": "discussion_contribution",
                    "delivery_channel": "handoff",
                    "requires_explicit_handoff": True,
                    "requires_process_events": True,
                    "requires_deliverable": True,
                },
                metadata={
                    "mode": self.mode,
                    "phase": "discussion",
                    "role": "participant",
                },
                position_x=-260 + (index * 260),
                position_y=220,
            )
            nodes.append(node)
            edges.append(TeamMissionEdgeSpec(from_node_id=root.node_id, to_node_id=node.node_id, kind="delegates"))
        if participants:
            synth = TeamMissionNodeSpec(
                node_id=_node_id(mission_id, "synthesis"),
                kind="synthesis",
                title="汇总讨论结果",
                objective=objective,
                status="todo",
                assignee_profile_id=leader.profile_id if leader else "",
                assignee_profile_version_id=leader.profile_version_id if leader else "",
                runtime_scope_key=f"team:{mission_id}:synthesis",
                output_contract={
                    "format": "final_answer",
                    "delivery_channel": "handoff",
                    "requires_explicit_handoff": True,
                    "requires_process_events": True,
                    "requires_deliverable": True,
                },
                metadata={"mode": self.mode, "phase": "synthesis", "role": "leader"},
                position_x=0,
                position_y=460,
            )
            nodes.append(synth)
            for participant in participants:
                edges.append(
                    TeamMissionEdgeSpec(
                        from_node_id=_node_id(mission_id, f"discussion-{participant.member_id}"),
                        to_node_id=synth.node_id,
                    )
                )
        return TeamMissionGraphPatch(
            mission_status="running",
            nodes=tuple(nodes),
            edges=tuple(edges),
            start_leader=True,
            auto_start_ready_nodes=True,
            metadata={"strategy": self.mode, "whole_graph_approval": False},
        )

    def should_start_leader(self) -> bool:
        return True

    def leader_start_text(
        self,
        *,
        mission_id: str,
        title: str,
        objective: str,
        members: Sequence[Mapping[str, Any] | TeamMissionMember] = (),
    ) -> str:
        del mission_id, title, members
        return "\n".join([
            "You are the DoXie team discussion leader.",
            "Keep the same persona, identity, tone, and memory as the underlying DoXie profile. Team mode only adds team context and team coordination tools.",
            "Never expose internal runtime, framework, or implementation names to the user. The product name shown to users is DoXie.",
            "Coordinate the discussion nodes and synthesize the result. Do not treat this as a single-agent direct answer unless no participants exist.",
            "",
            f"Mission objective: {str(objective or '').strip()}",
        ]).strip()

    def allow_automatic_graph_mutation(self, *, phase: str = "") -> bool:
        return phase in {"discussion", "synthesis", ""}

    def risk_policy(self, *, risk_level: str = "low", action_kind: str = "") -> RiskDecision:
        if str(risk_level or "low") in {"high", "critical"}:
            return RiskDecision("deny", "discussion mode cannot perform high risk execution", "mission")
        return RiskDecision("allow", "discussion contribution is allowed")


class SupervisedMissionStrategy(TeamMissionModeStrategy):
    mode = MODE_SUPERVISED_MISSION

    def initialize_graph(
        self,
        *,
        mission_id: str,
        title: str,
        objective: str,
        members: Sequence[Mapping[str, Any] | TeamMissionMember] = (),
        graph_payload: Mapping[str, Any] | None = None,
    ) -> TeamMissionGraphPatch:
        leader = _leader_member(_normalize_members(members))
        return TeamMissionGraphPatch(
            mission_status="planning",
            nodes=(
                self._root_node(
                    mission_id=mission_id,
                    title=title,
                    objective=objective,
                    status="running",
                    phase="planning",
                    leader=leader,
                ),
            ),
            start_leader=True,
            requires_whole_graph_approval=True,
            metadata={"strategy": self.mode, "whole_graph_approval": True},
        )

    def should_start_leader(self) -> bool:
        return True

    def leader_start_text(
        self,
        *,
        mission_id: str,
        title: str,
        objective: str,
        members: Sequence[Mapping[str, Any] | TeamMissionMember] = (),
    ) -> str:
        del members
        profile_hint = _team_profile_prompt_hint()
        return "\n".join([
            "You are the DoXie team Leader Planner for supervised execution.",
            "Keep the same persona, identity, tone, and memory as the underlying DoXie profile. Team mode only adds team context and team coordination tools.",
            "Never expose internal runtime, framework, or implementation names to the user. The product name shown to users is DoXie.",
            "Your current phase is planning only. Do not execute the user task directly and do not provide the final answer.",
            "Stream your understanding and decomposition for the user while you build the mission graph.",
            "The current task title/objective below are authoritative. Ignore older draft mission text, greetings, and memory if they conflict.",
            "",
            f"Mission id: {mission_id}",
            f"Mission title: {str(title or '').strip()}",
            f"Mission objective: {str(objective or '').strip()}",
            profile_hint,
            "",
            "What you can and cannot do during planning:",
            "- WRITE the task graph via team_mission_node_create / team_mission_edge_create / team_mission_plan_complete.",
            "- ASK the user via the clarify tool when a critical input is missing or ambiguous.",
            "- READ the workspace via read_file / parse_document / search_files to make the graph match reality.",
            "- You MUST NOT write files, run commands, or execute the task. Those happen in worker nodes AFTER the user approves the graph.",
            "- You MUST NOT create an approval_gate node yourself. The runtime inserts the whole-graph approval gate automatically when you call team_mission_plan_complete.",
            "",
            "Required graph protocol:",
            "1. Review team/member capabilities with team_mission_team_profile before deciding assignees.",
            "2. If a critical parameter is missing or ambiguous (target file, value to set, scope, success criteria, etc.), CALL the clarify tool now. Do NOT just describe the gap in prose and stop — that leaves the mission stuck with nothing to approve or execute.",
            "3. Use bounded graph tools only: team_mission_graph_summary for overview and team_mission_graph_slice for selected nodes. Never ask for or recreate the full graph in prose.",
            "4. Use team_mission_node_create to create ONE worker/verifier/synthesis node per tool call. Keep each tool call small; do not batch multiple nodes in one JSON argument.",
            "5. Include a stable idempotency_key for every node/edge mutation so retries cannot duplicate graph items.",
            "6. Use team_mission_edge_create to connect dependencies between the planned nodes.",
            "7. Keep executable worker nodes in ready/todo states; do not start them.",
            "8. When the whole graph is planned, call team_mission_plan_complete.",
            "9. After plan completion, stop. The user approval gate will release execution later.",
            "",
            "Required node brief contract:",
            "- Every worker, verifier, and synthesis node MUST include task_brief in team_mission_node_create.",
            "- task_brief.background: the relevant user context, source material, dependency context, constraints, and why this node exists.",
            "- task_brief.execution: concrete work items the assignee must perform, not generic labels like 'handle this'.",
            "- task_brief.goal: the outcome this node owns.",
            "- task_brief.acceptance_criteria: observable done checks the Leader/user can verify.",
            "- Put optional inputs, deliverables, and constraints into task_brief when available.",
            "- Keep each task_brief bounded and concrete. If a node needs long source material, reference the file/artifact/input instead of pasting it into task_brief.",
            "- If you cannot fill any required task_brief field from the user request or workspace inspection, use clarify before creating the node. Do not guess and do not create vague nodes.",
            "",
            "Termination contract: every planning run MUST end in exactly one of these states:",
            "  (a) at least one clarify request is open and awaiting the user's answer, or",
            "  (b) team_mission_plan_complete has been called with at least one worker node plus explicit verifier and synthesis nodes.",
            "Ending with neither (writing a question or plan as prose and stopping) leaves the mission stuck running with nothing to approve — never do that.",
        ]).strip()

    def requires_whole_graph_approval(self) -> bool:
        return True

    def allow_automatic_graph_mutation(self, *, phase: str = "") -> bool:
        return phase in {"planning", "change_request"}

    def on_plan_completed(
        self,
        *,
        mission_id: str,
        planned_nodes: Sequence[TeamMissionNodeSpec] = (),
        planned_edges: Sequence[TeamMissionEdgeSpec] = (),
    ) -> TeamMissionStrategyActions:
        work_nodes = tuple(
            node for node in planned_nodes
            if normalize_team_mission_node_kind(node.kind) not in {"root", "approval_gate"}
        )
        approval = TeamMissionNodeSpec(
            node_id=_node_id(mission_id, "approval-plan"),
            kind="approval_gate",
            title="审批任务图",
            objective="用户审批 Leader 规划的完整任务图后才能执行成员节点。",
            status="waiting_approval",
            output_contract={"format": "approval_request"},
            metadata={"mode": self.mode, "approval_scope": "whole_graph"},
            position_x=0,
            position_y=240,
        )
        existing_edges = {
            (edge.from_node_id, edge.to_node_id, edge.kind)
            for edge in planned_edges
        }
        root_to_approval = TeamMissionEdgeSpec(
            from_node_id=_node_id(mission_id, "root"),
            to_node_id=approval.node_id,
            kind="depends_on",
            metadata={"approval_gate": True, "approval_scope": "whole_graph"},
        )
        approval_edges = (root_to_approval,) + tuple(
            TeamMissionEdgeSpec(
                from_node_id=approval.node_id,
                to_node_id=node.node_id,
                kind="depends_on",
                metadata={"approval_gate": True, "approval_scope": "whole_graph"},
            )
            for node in work_nodes
            if (approval.node_id, node.node_id, "depends_on") not in existing_edges
        )
        return TeamMissionStrategyActions(
            mission_status="waiting_approval",
            nodes=tuple(planned_nodes) + (approval,),
            edges=tuple(planned_edges) + approval_edges,
            approval_requests=(
                {
                    "approval_id": approval.node_id,
                    "scope": "whole_graph",
                    "reason": "supervised mission requires user approval before execution",
                },
            ),
            events=(
                {
                    "type": "mission.approval.requested",
                    "payload": {
                        "approval_id": approval.node_id,
                        "scope": "whole_graph",
                        "node_id": approval.node_id,
                    },
                },
            ),
            auto_start_ready_nodes=False,
        )

    def risk_policy(self, *, risk_level: str = "low", action_kind: str = "") -> RiskDecision:
        if str(risk_level or "low") in {"high", "critical"}:
            return RiskDecision("require_approval", "high risk action requires secondary approval")
        return RiskDecision("allow", "approved graph low risk action")


class AutonomousMissionStrategy(TeamMissionModeStrategy):
    mode = MODE_AUTONOMOUS_MISSION

    def initialize_graph(
        self,
        *,
        mission_id: str,
        title: str,
        objective: str,
        members: Sequence[Mapping[str, Any] | TeamMissionMember] = (),
        graph_payload: Mapping[str, Any] | None = None,
    ) -> TeamMissionGraphPatch:
        leader = _leader_member(_normalize_members(members))
        return TeamMissionGraphPatch(
            mission_status="planning",
            nodes=(
                self._root_node(
                    mission_id=mission_id,
                    title=title,
                    objective=objective,
                    status="running",
                    phase="planning",
                    leader=leader,
                ),
            ),
            start_leader=True,
            auto_start_ready_nodes=False,
            metadata={"strategy": self.mode, "whole_graph_approval": False},
        )

    def should_start_leader(self) -> bool:
        return True

    def leader_start_text(
        self,
        *,
        mission_id: str,
        title: str,
        objective: str,
        members: Sequence[Mapping[str, Any] | TeamMissionMember] = (),
    ) -> str:
        del members
        profile_hint = _team_profile_prompt_hint()
        return "\n".join([
            "You are the DoXie team Leader Planner for autonomous execution.",
            "Keep the same persona, identity, tone, and memory as the underlying DoXie profile. Team mode only adds team context and team coordination tools.",
            "Never expose internal runtime, framework, or implementation names to the user. The product name shown to users is DoXie.",
            "Your first phase is planning. Do not execute worker tasks inside the root planning node.",
            "Build the mission graph before execution is released.",
            "The current task title/objective below are authoritative. Ignore older draft mission text, greetings, and memory if they conflict.",
            "",
            f"Mission id: {mission_id}",
            f"Mission title: {str(title or '').strip()}",
            f"Mission objective: {str(objective or '').strip()}",
            profile_hint,
            "",
            "Required graph protocol:",
            "1. Review team/member capabilities with team_mission_team_profile before deciding assignees and creating the task graph.",
            "2. Use bounded graph tools only: team_mission_graph_summary for overview and team_mission_graph_slice for selected nodes.",
            "3. Use team_mission_node_create to create ONE worker/verifier/synthesis node per tool call. Keep each tool call small; do not batch multiple nodes in one JSON argument.",
            "4. Include a stable idempotency_key for every node/edge mutation so retries cannot duplicate graph items.",
            "5. Use team_mission_edge_create to connect dependencies.",
            "6. Mark high-risk nodes with metadata.risk_level='high' or 'critical'.",
            "7. When the graph includes at least one worker node plus explicit verifier and synthesis nodes, call team_mission_plan_complete so DoXie can release low-risk ready nodes.",
            "",
            "Required node brief contract:",
            "- Every worker, verifier, and synthesis node MUST include task_brief in team_mission_node_create.",
            "- task_brief.background: the relevant user context, source material, dependency context, constraints, and why this node exists.",
            "- task_brief.execution: concrete work items the assignee must perform.",
            "- task_brief.goal: the outcome this node owns.",
            "- task_brief.acceptance_criteria: observable done checks the Leader/user can verify.",
            "- Keep each task_brief bounded and concrete. If a node needs long source material, reference the file/artifact/input instead of pasting it into task_brief.",
            "- If any required field is unclear, call clarify before creating the node. Autonomous mode does not mean executing on guessed assumptions.",
        ]).strip()

    def allow_automatic_graph_mutation(self, *, phase: str = "") -> bool:
        return phase in {"planning", "running", "recovery", ""}

    def on_plan_completed(
        self,
        *,
        mission_id: str,
        planned_nodes: Sequence[TeamMissionNodeSpec] = (),
        planned_edges: Sequence[TeamMissionEdgeSpec] = (),
    ) -> TeamMissionStrategyActions:
        startable = tuple(
            node.node_id
            for node in planned_nodes
            if node.status == "ready" and node.metadata.get("risk_level", "low") not in {"high", "critical"}
        )
        return TeamMissionStrategyActions(
            mission_status="running",
            nodes=tuple(planned_nodes),
            edges=tuple(planned_edges),
            start_node_ids=startable,
            auto_start_ready_nodes=True,
        )

    def select_ready_nodes(self, graph: Mapping[str, Any]) -> tuple[str, ...]:
        nodes = graph.get("nodes") if isinstance(graph, Mapping) else []
        nodes_by_id: dict[str, Mapping[str, Any]] = {}
        for node in nodes if isinstance(nodes, Sequence) else []:
            if not isinstance(node, Mapping):
                continue
            node_id = str(node.get("node_id") or "")
            if node_id:
                nodes_by_id[node_id] = node
        selected: list[str] = []
        for node_id in super().select_ready_nodes(graph):
            node = nodes_by_id.get(str(node_id or ""))
            metadata = node.get("metadata") if isinstance(node, Mapping) and isinstance(node.get("metadata"), Mapping) else {}
            risk_level = str(metadata.get("risk_level") or "low").strip().lower()
            if risk_level in {"high", "critical"}:
                continue
            selected.append(node_id)
        return tuple(selected)

    def risk_policy(self, *, risk_level: str = "low", action_kind: str = "") -> RiskDecision:
        if str(risk_level or "low") in {"high", "critical"}:
            return RiskDecision("require_approval", "autonomous mode requires local approval for high risk action")
        return RiskDecision("allow", "autonomous low risk action")


class ManualGraphStrategy(TeamMissionModeStrategy):
    mode = MODE_MANUAL_GRAPH

    def initialize_graph(
        self,
        *,
        mission_id: str,
        title: str,
        objective: str,
        members: Sequence[Mapping[str, Any] | TeamMissionMember] = (),
        graph_payload: Mapping[str, Any] | None = None,
    ) -> TeamMissionGraphPatch:
        nodes, edges = _manual_graph_specs(mission_id, graph_payload or {})
        return TeamMissionGraphPatch(
            mission_status="ready" if nodes else "draft",
            nodes=tuple(nodes),
            edges=tuple(edges),
            start_leader=False,
            auto_start_ready_nodes=False,
            metadata={"strategy": self.mode, "whole_graph_approval": False, "user_authored_graph": True},
        )

    def should_start_leader(self) -> bool:
        return False

    def allow_automatic_graph_mutation(self, *, phase: str = "") -> bool:
        return False

    def can_mutate_graph(self, *, actor: str, phase: str = "") -> bool:
        return actor in {"user", "external_api"}

    def on_plan_completed(
        self,
        *,
        mission_id: str,
        planned_nodes: Sequence[TeamMissionNodeSpec] = (),
        planned_edges: Sequence[TeamMissionEdgeSpec] = (),
    ) -> TeamMissionStrategyActions:
        return TeamMissionStrategyActions(mission_status="ready")


def strategy_for_mode(mode: str) -> TeamMissionModeStrategy:
    normalized = str(mode or MODE_SUPERVISED_MISSION).strip() or MODE_SUPERVISED_MISSION
    if normalized == MODE_DISCUSSION:
        return DiscussionStrategy()
    if normalized == MODE_SUPERVISED_MISSION:
        return SupervisedMissionStrategy()
    if normalized == MODE_AUTONOMOUS_MISSION:
        return AutonomousMissionStrategy()
    if normalized == MODE_MANUAL_GRAPH:
        return ManualGraphStrategy()
    raise ValueError(f"unsupported team mission mode: {normalized}")


def _normalize_members(
    members: Sequence[Mapping[str, Any] | TeamMissionMember],
) -> tuple[TeamMissionMember, ...]:
    normalized: list[TeamMissionMember] = []
    for raw in members:
        member = TeamMissionMember.from_raw(raw)
        if member.member_id and member.status not in {"disabled", "removed"}:
            normalized.append(member)
    return tuple(normalized)


def _leader_member(members: Sequence[TeamMissionMember]) -> TeamMissionMember | None:
    for member in members:
        if member.role in {"leader", "lead"}:
            return member
    return members[0] if members else None


def _worker_members(members: Sequence[TeamMissionMember]) -> tuple[TeamMissionMember, ...]:
    return tuple(member for member in members if member.role not in {"leader", "lead"})


def _team_profile_prompt_hint() -> str:
    return "\n".join([
        "Team capability guidance:",
        "1. Use team_mission_team_profile to understand the current team and member capabilities; do not infer capabilities from prompt text.",
        "2. Select worker assignee_member_id only from team_mission_team_profile.snapshot.member_profiles[].member_id.",
        "3. Never use run_id, session_id, node_id, profile_id, or profile_version_id as assignee_member_id.",
        "4. For verifier, synthesis, approval, and orchestration nodes, omit assignee fields unless the profile tool result identifies the Leader member_id.",
        "5. Use canonical node kinds only: worker, verifier, synthesis. Root and approval_gate are reserved for the DoXie team runtime.",
        "6. Put specialties such as research, coding, testing, quality, or verification in metadata.work_type or output_contract; do not use them as kind.",
    ])


def _node_id(mission_id: str, suffix: str) -> str:
    return f"team-mission:{mission_id}:{suffix}"


def _manual_graph_specs(
    mission_id: str,
    graph_payload: Mapping[str, Any],
) -> tuple[list[TeamMissionNodeSpec], list[TeamMissionEdgeSpec]]:
    raw_nodes = graph_payload.get("nodes") if isinstance(graph_payload, Mapping) else []
    raw_edges = graph_payload.get("edges") if isinstance(graph_payload, Mapping) else []
    nodes: list[TeamMissionNodeSpec] = []
    known_node_ids: set[str] = set()
    if isinstance(raw_nodes, Sequence) and not isinstance(raw_nodes, (str, bytes)):
        for index, raw_node in enumerate(raw_nodes):
            if not isinstance(raw_node, Mapping):
                continue
            node_id = str(raw_node.get("node_id") or raw_node.get("id") or _node_id(mission_id, f"manual-{index}")).strip()
            if not node_id:
                continue
            raw_kind = str(raw_node.get("kind") or "worker")
            kind = normalize_team_mission_node_kind(raw_kind)
            metadata = metadata_with_normalized_node_kind(
                {
                    **dict(raw_node.get("metadata") or {}),
                    "mode": MODE_MANUAL_GRAPH,
                    **(
                        {"assignee_member_id": str(raw_node.get("assignee_member_id") or raw_node.get("assigneeMemberId") or "")}
                        if raw_node.get("assignee_member_id") or raw_node.get("assigneeMemberId")
                        else {}
                    ),
                },
                raw_kind=raw_kind,
                canonical_kind=kind,
            )
            known_node_ids.add(node_id)
            nodes.append(
                TeamMissionNodeSpec(
                    node_id=node_id,
                    kind=kind,
                    title=str(raw_node.get("title") or f"任务节点 {index + 1}"),
                    objective=str(raw_node.get("objective") or raw_node.get("description") or ""),
                    status=str(raw_node.get("status") or "ready"),
                    assignee_profile_id=str(raw_node.get("assignee_profile_id") or raw_node.get("assigneeProfileId") or ""),
                    assignee_profile_version_id=str(
                        raw_node.get("assignee_profile_version_id") or raw_node.get("assigneeProfileVersionId") or ""
                    ),
                    runtime_scope_key=str(raw_node.get("runtime_scope_key") or raw_node.get("runtimeScopeKey") or ""),
                    output_contract=dict(raw_node.get("output_contract") or raw_node.get("outputContract") or {}),
                    metadata=metadata,
                    position_x=float(raw_node.get("position_x") or raw_node.get("x") or 0),
                    position_y=float(raw_node.get("position_y") or raw_node.get("y") or 0),
                )
            )
    edges: list[TeamMissionEdgeSpec] = []
    if isinstance(raw_edges, Sequence) and not isinstance(raw_edges, (str, bytes)):
        for raw_edge in raw_edges:
            if not isinstance(raw_edge, Mapping):
                continue
            from_node_id = str(raw_edge.get("from_node_id") or raw_edge.get("source") or "").strip()
            to_node_id = str(raw_edge.get("to_node_id") or raw_edge.get("target") or "").strip()
            if not from_node_id or not to_node_id:
                continue
            if known_node_ids and (from_node_id not in known_node_ids or to_node_id not in known_node_ids):
                raise ValueError("manual graph edge references an unknown node")
            edges.append(
                TeamMissionEdgeSpec(
                    edge_id=str(raw_edge.get("edge_id") or raw_edge.get("id") or ""),
                    from_node_id=from_node_id,
                    to_node_id=to_node_id,
                    kind=str(raw_edge.get("kind") or "depends_on"),
                    metadata=dict(raw_edge.get("metadata") or {}),
                )
            )
    return nodes, edges
