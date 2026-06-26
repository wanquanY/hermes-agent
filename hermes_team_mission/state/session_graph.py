from __future__ import annotations

# ruff: noqa: F401,F403,F405
from hermes_state_runs import DEFAULT_ORPHANED_ACTIVE_RUN_OWNER_DEAD_GRACE_SECONDS
from hermes_state_runs import DEFAULT_ORPHANED_ACTIVE_RUN_STALE_SECONDS
from hermes_state_runs import orphaned_active_run_decision

from .session_common import *


class SessionDBTeamMissionGraphMixin:
    def upsert_team_mission(
        self,
        *,
        mission_id: str,
        conversation_id: str = "",
        team_id: str = "",
        title: str = "",
        objective: str = "",
        workspace_id: str = "",
        workspace_path: str = "",
        mode: str = "supervised_mission",
        status: str = "planning",
        leader_session_id: str = "",
        created_at: float | None = None,
        updated_at: float | None = None,
        completed_at: float | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            return {}
        now = time.time()
        created = float(created_at or now)
        updated = float(updated_at or now)

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            existing = conn.execute(
                "SELECT metadata_json, created_at, conversation_id FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
            merged_metadata = _json_loads(_row_value(existing, "metadata_json", ""), {})
            if isinstance(metadata, dict):
                merged_metadata.update(metadata)
            candidate_stable_session_id = _stable_session_id_from_metadata(
                merged_metadata,
                _text(leader_session_id or team_id or mission_id),
            )
            conversation_by_session = conn.execute(
                "SELECT conversation_id FROM team_mission_conversations WHERE stable_session_id = ?",
                (candidate_stable_session_id,),
            ).fetchone() if candidate_stable_session_id else None
            resolved_conversation_id = (
                _text(conversation_id)
                or _text(_row_value(existing, "conversation_id", ""))
                or _conversation_id_from_metadata(merged_metadata)
                or _text(_row_value(conversation_by_session, "conversation_id", ""))
                or mission_id
            )
            if resolved_conversation_id:
                merged_metadata["conversation_id"] = resolved_conversation_id
            conn.execute(
                """
                INSERT INTO team_missions (
                    mission_id, conversation_id, team_id, title, objective, workspace_id, workspace_path,
                    mode, status, leader_session_id, created_at, updated_at, completed_at,
                    metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mission_id) DO UPDATE SET
                    conversation_id = COALESCE(NULLIF(excluded.conversation_id, ''), conversation_id),
                    team_id = excluded.team_id,
                    title = excluded.title,
                    objective = excluded.objective,
                    workspace_id = excluded.workspace_id,
                    workspace_path = excluded.workspace_path,
                    mode = excluded.mode,
                    status = excluded.status,
                    leader_session_id = COALESCE(NULLIF(excluded.leader_session_id, ''), leader_session_id),
                    updated_at = excluded.updated_at,
                    completed_at = excluded.completed_at,
                    metadata_json = excluded.metadata_json
                """,
                (
                    mission_id,
                    resolved_conversation_id,
                    str(team_id or ""),
                    str(title or ""),
                    str(objective or ""),
                    str(workspace_id or ""),
                    str(workspace_path or ""),
                    str(mode or "supervised_mission"),
                    str(status or "planning"),
                    str(leader_session_id or ""),
                    float(_row_value(existing, "created_at", created) or created),
                    updated,
                    completed_at,
                    _json_dumps(merged_metadata if isinstance(merged_metadata, dict) else {}),
                ),
            )
            return self._team_mission_from_row(conn.execute(
                "SELECT * FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()) or {}

        mission = self._execute_write(_do)
        if mission:
            self.ensure_team_mission_conversation(mission=mission)
            refreshed = self.get_team_mission_graph(mission_id).get("mission")
            result = refreshed or mission
            self._prune_team_mission_events_if_terminal(mission_id)
            return result
        return mission

    def initialize_team_mission_from_strategy(
        self,
        *,
        mission_id: str,
        conversation_id: str = "",
        team_id: str = "",
        title: str = "",
        objective: str = "",
        workspace_id: str = "",
        workspace_path: str = "",
        mode: str = "supervised_mission",
        members: List[Dict[str, Any]] | None = None,
        graph_payload: Dict[str, Any] | None = None,
        leader_session_id: str = "",
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Create a Team Mission through the Hermes mode strategy contract.

        This is the native entry point for product modes. Callers provide the
        requested mode and optional manual graph payload; Hermes chooses the
        initial graph/status through ``TeamMissionModeStrategy`` and persists
        it in the Team Mission tables.
        """
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            return {}
        strategy = strategy_for_mode(mode)
        patch = strategy.initialize_graph(
            mission_id=mission_id,
            title=str(title or ""),
            objective=str(objective or ""),
            members=members or [],
            graph_payload=graph_payload or {},
        )
        mission_metadata = _mission_metadata_with_members({
            "mode_strategy": strategy.mode,
            "start_leader": patch.start_leader,
            "auto_start_ready_nodes": patch.auto_start_ready_nodes,
            "requires_whole_graph_approval": patch.requires_whole_graph_approval,
            **(patch.metadata or {}),
        }, members)
        if isinstance(metadata, dict):
            mission_metadata.update(metadata)
            mission_metadata = _mission_metadata_with_members(mission_metadata, members)
        self.upsert_team_mission(
            mission_id=mission_id,
            conversation_id=conversation_id,
            team_id=team_id,
            title=title,
            objective=objective,
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            mode=strategy.mode,
            status=patch.mission_status,
            leader_session_id=leader_session_id,
            metadata=mission_metadata,
        )
        for node in patch.nodes:
            self.upsert_team_mission_node(
                mission_id=mission_id,
                node_id=node.node_id,
                kind=node.kind,
                title=node.title,
                objective=node.objective,
                status=node.status,
                assignee_profile_id=node.assignee_profile_id,
                assignee_profile_version_id=node.assignee_profile_version_id,
                runtime_scope_key=node.runtime_scope_key,
                output_contract=node.output_contract,
                metadata=node.metadata,
                position_x=node.position_x,
                position_y=node.position_y,
            )
        for edge in patch.edges:
            self.upsert_team_mission_edge(
                mission_id=mission_id,
                edge_id=edge.edge_id,
                from_node_id=edge.from_node_id,
                to_node_id=edge.to_node_id,
                kind=edge.kind,
                metadata=edge.metadata,
            )
        # Light up the sidebar immediately when a team task starts. Without this
        # the conversation's session_index row stays running=0 until something
        # else triggers a status projection — and nothing does for the initial
        # planning state (reduce_team_mission_graph only projects on a status
        # CHANGE, and the leader's root planning run uses a node session id
        # team:mission-X:node:root that does NOT match the conversation's
        # session_index row, so the run-write projection updates a different
        # row). Result: starting a new team task left the sidebar idle until
        # synthesis finally landed.
        ms = str(patch.mission_status or "").lower()
        if ms in _TERMINAL_MISSION_STATUSES:
            idx_status, idx_running, idx_waiting = "idle", False, False
        elif ms == "waiting_approval":
            idx_status, idx_running, idx_waiting = "waiting_approval", False, True
        elif ms:
            idx_status, idx_running, idx_waiting = "running", True, False
        else:
            idx_status, idx_running, idx_waiting = "", False, False
        _log.debug(
            "[doxie-session-index] initialize_mission mission_id=%s patch_status=%s idx_status=%s idx_running=%s",
            mission_id, ms, idx_status, idx_running,
        )
        if idx_status:
            try:
                self.update_session_index_for_mission(
                    mission_id,
                    status=idx_status,
                    running=idx_running,
                    waiting_approval=idx_waiting,
                )
            except Exception as exc:
                _log.warning(
                    "[doxie-session-index] initialize_mission projection FAILED mission_id=%s error=%s",
                    mission_id, exc,
                )
        return self.get_team_mission_graph(mission_id)

    def upsert_team_mission_node(
        self,
        *,
        mission_id: str,
        node_id: str,
        kind: str = "worker",
        title: str = "",
        objective: str = "",
        status: str = "todo",
        assignee_profile_id: str = "",
        assignee_profile_version_id: str = "",
        canonical_node_id: str = "",
        task_frame_id: str = "",
        runtime_stable_session_id: str = "",
        runtime_session_id: str = "",
        runtime_scope_key: str = "",
        output_contract: Dict[str, Any] | None = None,
        metadata: Dict[str, Any] | None = None,
        position_x: float = 0,
        position_y: float = 0,
        created_at: float | None = None,
        updated_at: float | None = None,
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        node_id = str(node_id or "").strip()
        if not mission_id or not node_id:
            return {}
        now = time.time()
        created = float(created_at or now)
        updated = float(updated_at or now)
        raw_kind = str(kind or "worker")
        canonical_kind = _normalize_node_kind(raw_kind)

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            existing = conn.execute(
                "SELECT * FROM team_mission_nodes WHERE mission_id = ? AND node_id = ?",
                (mission_id, node_id),
            ).fetchone()
            merged_metadata = _json_loads(_row_value(existing, "metadata_json", ""), {})
            if isinstance(metadata, dict):
                merged_metadata.update(metadata)
            merged_metadata = _metadata_with_normalized_node_kind(
                merged_metadata if isinstance(merged_metadata, dict) else {},
                raw_kind=raw_kind,
                canonical_kind=canonical_kind,
            )
            mission_row = conn.execute(
                "SELECT * FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
            mission_metadata = _json_loads(_row_value(mission_row, "metadata_json", ""), {})
            leader_row = conn.execute(
                """
                SELECT * FROM team_mission_nodes
                 WHERE mission_id = ? AND kind = ?
                 ORDER BY created_at ASC
                 LIMIT 1
                """,
                (mission_id, "root"),
            ).fetchone()
            existing_node = self._team_mission_node_from_row(existing) or {}
            leader_node = self._team_mission_node_from_row(leader_row) or {}
            resolved_profile_id, resolved_profile_version_id, resolved_runtime_scope_key, resolved_metadata = _resolve_node_assignee(
                mission_id=mission_id,
                node_id=node_id,
                kind=canonical_kind,
                incoming_profile_id=str(assignee_profile_id or ""),
                incoming_profile_version_id=str(assignee_profile_version_id or ""),
                incoming_runtime_scope_key=str(runtime_scope_key or ""),
                metadata=merged_metadata if isinstance(merged_metadata, dict) else {},
                mission_metadata=mission_metadata if isinstance(mission_metadata, dict) else {},
                existing_node=existing_node,
                leader_node=leader_node,
            )
            existing_canonical_node_id = _text(_row_value(existing, "canonical_node_id", ""))
            existing_task_frame_id = _text(_row_value(existing, "task_frame_id", ""))
            existing_runtime_stable_session_id = _text(_row_value(existing, "runtime_stable_session_id", ""))
            existing_runtime_session_id = _text(_row_value(existing, "runtime_session_id", ""))
            effective_canonical_node_id = (
                _text(canonical_node_id)
                or existing_canonical_node_id
                or _conversation_graph_node_id(mission_id, node_id)
            )
            effective_task_frame_id = (
                _text(task_frame_id)
                or existing_task_frame_id
                or (f"mission-frame:{mission_id}" if mission_id else "")
            )
            effective_runtime_stable_session_id = (
                _text(runtime_stable_session_id)
                or existing_runtime_stable_session_id
            )
            effective_runtime_session_id = (
                _text(runtime_session_id)
                or existing_runtime_session_id
            )
            conn.execute(
                """
                INSERT INTO team_mission_nodes (
                    node_id, mission_id, kind, title, objective, status,
                    assignee_profile_id, assignee_profile_version_id,
                    canonical_node_id, task_frame_id, runtime_stable_session_id,
                    runtime_session_id, runtime_scope_key,
                    output_contract_json, metadata_json, position_x, position_y,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mission_id, node_id) DO UPDATE SET
                    kind = excluded.kind,
                    title = excluded.title,
                    objective = excluded.objective,
                    status = excluded.status,
                    assignee_profile_id = excluded.assignee_profile_id,
                    assignee_profile_version_id = excluded.assignee_profile_version_id,
                    canonical_node_id = excluded.canonical_node_id,
                    task_frame_id = excluded.task_frame_id,
                    runtime_stable_session_id = excluded.runtime_stable_session_id,
                    runtime_session_id = excluded.runtime_session_id,
                    runtime_scope_key = excluded.runtime_scope_key,
                    output_contract_json = excluded.output_contract_json,
                    metadata_json = excluded.metadata_json,
                    position_x = excluded.position_x,
                    position_y = excluded.position_y,
                    updated_at = excluded.updated_at
                """,
                (
                    node_id,
                    mission_id,
                    canonical_kind,
                    str(title or ""),
                    str(objective or ""),
                    str(status or "todo"),
                    resolved_profile_id,
                    resolved_profile_version_id,
                    effective_canonical_node_id,
                    effective_task_frame_id,
                    effective_runtime_stable_session_id,
                    effective_runtime_session_id,
                    resolved_runtime_scope_key,
                    _json_dumps(output_contract or {}),
                    _json_dumps(resolved_metadata),
                    float(position_x or 0),
                    float(position_y or 0),
                    float(_row_value(existing, "created_at", created) or created),
                    updated,
                ),
            )
            return self._team_mission_node_from_row(conn.execute(
                "SELECT * FROM team_mission_nodes WHERE mission_id = ? AND node_id = ?",
                (mission_id, node_id),
            ).fetchone()) or {}

        return self._execute_write(_do)

    def get_team_mission_node(self, mission_id: str, node_id: str) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        node_id = str(node_id or "").strip()
        if not mission_id or not node_id:
            return {}
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM team_mission_nodes WHERE mission_id = ? AND node_id = ?",
                (mission_id, node_id),
            ).fetchone()
            mission_row = self._conn.execute(
                "SELECT * FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
            leader_row = self._conn.execute(
                """
                SELECT * FROM team_mission_nodes
                 WHERE mission_id = ? AND kind = ?
                 ORDER BY created_at ASC
                 LIMIT 1
                """,
                (mission_id, "root"),
            ).fetchone()
            binding_row = self._conn.execute(
                """
                SELECT *
                FROM team_mission_run_bindings
                WHERE mission_id = ? AND node_id = ?
                ORDER BY updated_at DESC, created_at DESC, run_id DESC
                LIMIT 1
                """,
                (mission_id, node_id),
            ).fetchone()
        mission = self._team_mission_from_row(mission_row) or {}
        resolved_node = self._team_mission_node_with_resolved_assignee(
            self._team_mission_node_from_row(row) or {},
            mission_metadata=dict(mission.get("metadata") or {}),
            leader_node=self._team_mission_node_from_row(leader_row) or {},
        )
        return self._team_mission_node_with_runtime_binding(
            resolved_node,
            self._team_mission_run_binding_from_row(binding_row) or {},
        )

    def claim_team_mission_node_start(
        self,
        *,
        mission_id: str,
        node_id: str,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Atomically claim a ready node before submitting its runtime run."""
        mission_id = str(mission_id or "").strip()
        node_id = str(node_id or "").strip()
        if not mission_id or not node_id:
            return {}
        now = time.time()

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            row = conn.execute(
                "SELECT * FROM team_mission_nodes WHERE mission_id = ? AND node_id = ?",
                (mission_id, node_id),
            ).fetchone()
            node = self._team_mission_node_from_row(row)
            if not node:
                return {}
            if str(node.get("status") or "") != "ready":
                return node
            merged_metadata = dict(node.get("metadata") or {})
            if isinstance(metadata, dict):
                merged_metadata.update(metadata)
            merged_metadata["start_claimed_at"] = now
            cursor = conn.execute(
                """
                UPDATE team_mission_nodes
                   SET status = ?,
                       metadata_json = ?,
                       updated_at = ?
                 WHERE mission_id = ?
                   AND node_id = ?
                   AND status = ?
                """,
                (
                    "starting",
                    _json_dumps(merged_metadata),
                    now,
                    mission_id,
                    node_id,
                    "ready",
                ),
            )
            if cursor.rowcount <= 0:
                return self._team_mission_node_from_row(conn.execute(
                    "SELECT * FROM team_mission_nodes WHERE mission_id = ? AND node_id = ?",
                    (mission_id, node_id),
                ).fetchone()) or {}
            return self._team_mission_node_from_row(conn.execute(
                "SELECT * FROM team_mission_nodes WHERE mission_id = ? AND node_id = ?",
                (mission_id, node_id),
            ).fetchone()) or {}

        return self._execute_write(_do)

    def upsert_team_mission_edge(
        self,
        *,
        mission_id: str,
        from_node_id: str,
        to_node_id: str,
        edge_id: str = "",
        kind: str = "depends_on",
        metadata: Dict[str, Any] | None = None,
        created_at: float | None = None,
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        from_node_id = str(from_node_id or "").strip()
        to_node_id = str(to_node_id or "").strip()
        normalized_edge_id = str(edge_id or f"{mission_id}:{from_node_id}:{to_node_id}:{kind}").strip()
        if not mission_id or not from_node_id or not to_node_id:
            return {}
        created = float(created_at or time.time())

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            conn.execute(
                """
                INSERT INTO team_mission_edges (
                    edge_id, mission_id, from_node_id, to_node_id, kind, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(edge_id) DO UPDATE SET
                    mission_id = excluded.mission_id,
                    from_node_id = excluded.from_node_id,
                    to_node_id = excluded.to_node_id,
                    kind = excluded.kind,
                    metadata_json = excluded.metadata_json
                """,
                (
                    normalized_edge_id,
                    mission_id,
                    from_node_id,
                    to_node_id,
                    str(kind or "depends_on"),
                    _json_dumps(metadata or {}),
                    created,
                ),
            )
            return self._team_mission_edge_from_row(conn.execute(
                "SELECT * FROM team_mission_edges WHERE edge_id = ?",
                (normalized_edge_id,),
            ).fetchone()) or {}

        return self._execute_write(_do)

    def apply_team_mission_strategy_actions(
        self,
        *,
        mission_id: str,
        actions: TeamMissionStrategyActions,
        run_id: str = "",
        event_source: str = "strategy",
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            return {}
        graph = self.get_team_mission_graph(mission_id)
        mission = graph.get("mission") if isinstance(graph, dict) else None
        if not isinstance(mission, dict):
            return {}
        if actions.mission_status:
            self.upsert_team_mission(
                mission_id=mission_id,
                team_id=str(mission.get("team_id") or ""),
                title=str(mission.get("title") or ""),
                objective=str(mission.get("objective") or ""),
                workspace_id=str(mission.get("workspace_id") or ""),
                workspace_path=str(mission.get("workspace_path") or ""),
                mode=str(mission.get("mode") or ""),
                status=actions.mission_status,
                leader_session_id=str(mission.get("leader_session_id") or ""),
                metadata=dict(mission.get("metadata") or {}),
            )
        created_nodes = []
        for node in actions.nodes:
            created_nodes.append(
                self.upsert_team_mission_node(
                    mission_id=mission_id,
                    node_id=node.node_id,
                    kind=node.kind,
                    title=node.title,
                    objective=node.objective,
                    status=node.status,
                    assignee_profile_id=node.assignee_profile_id,
                    assignee_profile_version_id=node.assignee_profile_version_id,
                    runtime_scope_key=node.runtime_scope_key,
                    output_contract=node.output_contract,
                    metadata=node.metadata,
                    position_x=node.position_x,
                    position_y=node.position_y,
                )
            )
        created_edges = []
        for edge in actions.edges:
            created_edges.append(
                self.upsert_team_mission_edge(
                    mission_id=mission_id,
                    edge_id=edge.edge_id,
                    from_node_id=edge.from_node_id,
                    to_node_id=edge.to_node_id,
                    kind=edge.kind,
                    metadata=edge.metadata,
                )
            )
        if run_id:
            for event in actions.events:
                if isinstance(event, dict):
                    self.append_team_mission_run_event(
                        mission_id=mission_id,
                        run_id=run_id,
                        event=event,
                    )
            self.append_team_mission_run_event(
                mission_id=mission_id,
                run_id=run_id,
                event={
                    "type": "mission.strategy.actions",
                    "payload": {
                        "source": event_source,
                        "mission_status": actions.mission_status,
                        "nodes": created_nodes,
                        "edges": created_edges,
                        "start_node_ids": list(actions.start_node_ids),
                        "approval_requests": list(actions.approval_requests),
                        "auto_start_ready_nodes": actions.auto_start_ready_nodes,
                    },
                },
            )
        return self.get_team_mission_graph(mission_id)

    def _approval_actions_with_leader_assignee(
        self,
        mission_id: str,
        actions: TeamMissionStrategyActions,
    ) -> TeamMissionStrategyActions:
        """Stamp approval-gate node specs with the resolved leader assignee.

        The mode strategy creates the approval gate without an assignee. This
        backfills the leader/root node's already-resolved assignee (profile +
        member id + display name) onto each approval-gate spec so the approval
        node is owned by the real leader instead of the synthetic "Leader"
        placeholder when the mission members list is momentarily unavailable.
        """
        if not any(_normalize_node_kind(node.kind) == "approval_gate" for node in actions.nodes):
            return actions
        graph = self.get_team_mission_graph(mission_id)
        graph_nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
        leader_node = next(
            (
                node
                for node in graph_nodes
                if isinstance(node, dict) and _normalize_node_kind(node.get("kind")) == "root"
            ),
            None,
        )
        if not isinstance(leader_node, dict):
            return actions
        leader_profile_id = _text(leader_node.get("assignee_profile_id"))
        leader_profile_version_id = _text(leader_node.get("assignee_profile_version_id"))
        leader_metadata = leader_node.get("metadata") if isinstance(leader_node.get("metadata"), dict) else {}
        leader_member_id = _text(
            leader_metadata.get("assignee_member_id") or leader_metadata.get("assigneeMemberId")
        )
        # The synthetic placeholder owner uses member_id == "leader"; never
        # propagate it as if it were a real member.
        if leader_member_id == "leader" and not leader_profile_id:
            leader_member_id = ""
        leader_display_name = _text(
            leader_metadata.get("assignee_display_name") or leader_metadata.get("assigneeDisplayName")
        )
        # Nothing real to inherit (mission has no resolvable leader); leave the
        # spec untouched so existing fallback resolution still applies.
        if not leader_profile_id and not leader_member_id:
            return actions

        def _with_leader(node: TeamMissionNodeSpec) -> TeamMissionNodeSpec:
            if _normalize_node_kind(node.kind) != "approval_gate":
                return node
            metadata = dict(node.metadata or {})
            metadata.setdefault("role", "leader")
            metadata.setdefault("phase", "approval")
            if leader_member_id:
                metadata["assignee_member_id"] = leader_member_id
                metadata["assigneeMemberId"] = leader_member_id
            if leader_display_name:
                metadata.setdefault("assignee_display_name", leader_display_name)
                metadata.setdefault("assigneeDisplayName", leader_display_name)
            return replace(
                node,
                assignee_profile_id=node.assignee_profile_id or leader_profile_id,
                assignee_profile_version_id=node.assignee_profile_version_id or leader_profile_version_id,
                metadata=metadata,
            )

        def _with_leader_event(event: Dict[str, Any]) -> Dict[str, Any]:
            # The strategy builds the mission.approval.requested event before the
            # leader assignee is known (the mode has no DB access), so the LIVE event
            # carries no owner and the frontend renders the approval node with the
            # generic "Leader" placeholder until a graph reload picks up the stamped
            # node. Copy the resolved leader assignee into the event payload so the
            # live approval node shows the real leader member immediately.
            if not isinstance(event, dict) or _text(event.get("type")) != "mission.approval.requested":
                return event
            payload = dict(event.get("payload") or {})
            if leader_member_id:
                payload.setdefault("assignee_member_id", leader_member_id)
                payload.setdefault("assigneeMemberId", leader_member_id)
            if leader_profile_id:
                payload.setdefault("assignee_profile_id", leader_profile_id)
                payload.setdefault("agent_profile_id", leader_profile_id)
                payload.setdefault("agentProfileId", leader_profile_id)
            if leader_profile_version_id:
                payload.setdefault("assignee_profile_version_id", leader_profile_version_id)
            if leader_display_name:
                payload.setdefault("assignee_display_name", leader_display_name)
                payload.setdefault("assigneeDisplayName", leader_display_name)
            return {**event, "payload": payload}

        return replace(
            actions,
            nodes=tuple(_with_leader(node) for node in actions.nodes),
            events=tuple(_with_leader_event(event) for event in actions.events),
        )

    def complete_team_mission_plan(
        self,
        *,
        mission_id: str,
        run_id: str = "",
        task_id: str = "",
        event_source: str = "plan.complete",
    ) -> Dict[str, Any]:
        graph = self.get_team_mission_graph(mission_id)
        mission = graph.get("mission") if isinstance(graph, dict) else None
        if not isinstance(mission, dict):
            return {}
        strategy = strategy_for_mode(str(mission.get("mode") or "supervised_mission"))
        normalized_task_id = _text(task_id)
        if not normalized_task_id and run_id:
            binding = self.get_team_mission_run_binding(run_id)
            bound_node = self.get_team_mission_node(
                str(binding.get("mission_id") or mission_id),
                str(binding.get("node_id") or ""),
            )
            normalized_task_id = _task_id_from_node_and_binding(bound_node, binding)
        planned_nodes = tuple(
            _node_spec_from_graph_node(node)
            for node in graph.get("nodes", [])
            if isinstance(node, dict)
            and str(node.get("kind") or "") not in {"root", "approval_gate"}
            and _node_matches_task(node, normalized_task_id)
        )
        planned_node_ids = {
            node.node_id
            for node in planned_nodes
            if node.node_id
        }
        planned_edges = tuple(
            _edge_spec_from_graph_edge(edge)
            for edge in graph.get("edges", [])
            if isinstance(edge, dict)
            and _edge_matches_task(edge, planned_node_ids, normalized_task_id)
        )
        actions = strategy.on_plan_completed(
            mission_id=mission_id,
            planned_nodes=planned_nodes,
            planned_edges=planned_edges,
        )
        actions = _strategy_actions_with_task_id(actions, normalized_task_id)
        # Approval-gate nodes are leader-owned. The mode strategy has no DB
        # access, so it cannot stamp the real leader assignee on the approval
        # node and leaves it blank. If we persist it blank and the mission
        # members list is not resolvable at that instant, assignee resolution
        # falls back to the synthetic "Leader" placeholder and the approval node
        # appears undispatched. Seed the approval node spec with the leader/root
        # node's already-resolved assignee so it is owned by the real leader.
        actions = self._approval_actions_with_leader_assignee(mission_id, actions)
        updated_graph = self.apply_team_mission_strategy_actions(
            mission_id=mission_id,
            actions=actions,
            run_id=run_id,
            event_source=event_source,
        )
        reduced = self.reduce_team_mission_graph(mission_id)
        return {
            "mission_id": mission_id,
            "mission_status": actions.mission_status,
            "approval_requests": list(actions.approval_requests),
            "auto_start_ready_nodes": actions.auto_start_ready_nodes,
            "actions": actions,
            "graph": reduced.get("graph") if isinstance(reduced, dict) and reduced.get("graph") else updated_graph,
        }

    def reject_team_mission_plan(
        self,
        *,
        mission_id: str,
        task_id: str = "",
        rejected_by: str = "",
        reason: str = "",
        run_id: str = "",
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            return {}
        graph = self.get_team_mission_graph(mission_id)
        mission = graph.get("mission") if isinstance(graph, dict) else None
        if not isinstance(mission, dict):
            return {}
        nodes = [node for node in graph.get("nodes", []) if isinstance(node, dict)]
        normalized_task_id = _text(task_id)
        if not normalized_task_id:
            root_nodes = [
                node for node in nodes
                if str(node.get("kind") or "") == "root"
                and _task_id_from_node_and_binding(node)
            ]
            root_nodes.sort(key=lambda node: float(node.get("created_at") or node.get("updated_at") or 0))
            normalized_task_id = _task_id_from_node_and_binding(root_nodes[-1]) if root_nodes else ""
        selected_nodes = [
            node for node in nodes
            if (
                normalized_task_id
                and _task_id_from_node_and_binding(node) == normalized_task_id
            )
        ]
        if not selected_nodes:
            selected_nodes = [
                node for node in nodes
                if str(node.get("kind") or "") in {"root", "approval_gate"}
                or str(node.get("status") or "") in {"ready", "todo", "waiting_dependency", "blocked_waiting_dependency", "waiting_approval"}
            ]
        canceled_nodes = []
        for node in selected_nodes:
            canceled_nodes.append(self.upsert_team_mission_node(
                mission_id=mission_id,
                node_id=str(node.get("node_id") or ""),
                kind=str(node.get("kind") or "worker"),
                title=str(node.get("title") or ""),
                objective=str(node.get("objective") or ""),
                status="cancelled",
                assignee_profile_id=str(node.get("assignee_profile_id") or ""),
                assignee_profile_version_id=str(node.get("assignee_profile_version_id") or ""),
                runtime_scope_key=str(node.get("runtime_scope_key") or ""),
                output_contract=dict(node.get("output_contract") or {}),
                metadata={
                    **dict(node.get("metadata") or {}),
                    "rejected_by": _text(rejected_by),
                    "rejected_reason": _text(reason),
                    **({"task_id": normalized_task_id} if normalized_task_id else {}),
                },
                position_x=float(node.get("position_x") or 0),
                position_y=float(node.get("position_y") or 0),
            ))
        self.upsert_team_mission(
            mission_id=mission_id,
            team_id=str(mission.get("team_id") or ""),
            title=str(mission.get("title") or ""),
            objective=str(mission.get("objective") or ""),
            workspace_id=str(mission.get("workspace_id") or ""),
            workspace_path=str(mission.get("workspace_path") or ""),
            mode=str(mission.get("mode") or ""),
            status="draft",
            leader_session_id=str(mission.get("leader_session_id") or ""),
            metadata=dict(mission.get("metadata") or {}),
        )
        self.update_session_index_for_mission(
            mission_id, status="idle", running=False, waiting_approval=False,
        )
        event = {
            "type": "mission.plan.rejected",
            "timestamp": time.time(),
            "payload": {
                "task_id": normalized_task_id,
                "node_ids": [str(node.get("node_id") or "") for node in canceled_nodes],
                "rejected_by": _text(rejected_by),
                "reason": _text(reason),
                "mission_status": "draft",
            },
        }
        if run_id:
            self.append_team_mission_run_event(
                mission_id=mission_id,
                run_id=run_id,
                event=event,
            )
        else:
            self._append_team_mission_state_projection_event(
                mission_id=mission_id,
                event=event,
                identity={
                    "mission_id": mission_id,
                    "missionId": mission_id,
                    **({"task_id": normalized_task_id, "taskId": normalized_task_id} if normalized_task_id else {}),
                },
                dedupe_key=f"mission-plan-rejected:{mission_id}:{normalized_task_id or 'whole-graph'}",
            )
        return {
            "mission_id": mission_id,
            "task_id": normalized_task_id,
            "canceled_nodes": canceled_nodes,
            "graph": self.get_team_mission_graph(mission_id),
        }

    def cancel_team_mission(
        self,
        *,
        mission_id: str,
        canceled_by: str = "",
        reason: str = "",
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        if not mission_id:
            _log.warning("[doxie-cancel] cancel_team_mission ENTRY empty_mission_id")
            return {}
        graph = self.get_team_mission_graph(mission_id)
        mission = graph.get("mission") if isinstance(graph, dict) else None
        if not isinstance(mission, dict):
            _log.warning("[doxie-cancel] cancel_team_mission ABORT mission_id=%s mission_not_dict", mission_id)
            return {}
        _log.debug(
            "[doxie-cancel] cancel_team_mission ENTRY mission_id=%s current_status=%s node_count=%s",
            mission_id, _text(mission.get("status")), len(graph.get("nodes", []) or []),
        )
        mission_status = _text(mission.get("status")).lower()
        nodes = [node for node in graph.get("nodes", []) if isinstance(node, dict)]
        bindings = [binding for binding in graph.get("run_bindings", []) if isinstance(binding, dict)]
        canceled_at = time.time()

        def _mark_conversation_mission_cancelled() -> None:
            conversation_id = _text(mission.get("conversation_id"))
            if not conversation_id:
                return
            if self.set_conversation_mission_status(
                conversation_id=conversation_id,
                mission_id=mission_id,
                status="cancelled",
            ):
                return
            self.add_mission_to_conversation(
                conversation_id=conversation_id,
                mission_id=mission_id,
                status="cancelled",
            )

        # Safety reaper: re-read the *current* run status for EVERY run bound to
        # this mission (not just a stale graph snapshot) and force any run that
        # is not already terminal to a terminal status in the control-plane DB.
        # This guarantees no member-node worker run can survive a mission cancel
        # as a zombie "running" run, even if the scheduler started it around or
        # after the cancel.
        cancel_run_bindings: list[Dict[str, Any]] = []
        active_run_ids: set[str] = set()
        for binding in bindings:
            run_id = _text(binding.get("run_id"))
            if not run_id or run_id in active_run_ids:
                continue
            run = self.get_run(run_id) if hasattr(self, "get_run") else None
            run_status = _text((run or {}).get("status")).lower()
            if run and run_status not in _TERMINAL_RUN_STATUSES:
                active_run_ids.add(run_id)
                cancel_run_bindings.append(binding)
                if hasattr(self, "upsert_run"):
                    # Reap the run to a terminal status so the control-plane DB
                    # can never report it as 'running' after a cancel. The
                    # gateway still issues run.cancel for live worker
                    # termination; this is the durable backstop.
                    self.upsert_run(
                        run_id=run_id,
                        session_id=_text(run.get("session_id")) or _text(binding.get("session_id")),
                        runtime_scope_key=_text(run.get("runtime_scope_key")) or _text(binding.get("runtime_scope_key")),
                        turn_id=_text(run.get("turn_id")),
                        runtime_session_id=_text(run.get("runtime_session_id")) or _text(binding.get("runtime_session_id")),
                        status="cancelled",
                        completed_at=canceled_at,
                        metadata={
                            "cancelled_by": _text(canceled_by) or "team_mission.cancel",
                            "cancel_reason": _text(reason),
                            "cancelled_mission_id": mission_id,
                        },
                    )

        if mission_status in _TERMINAL_MISSION_STATUSES:
            # Mission is already terminal, but we still return (and have just
            # reaped) any runs that were left non-terminal so the gateway can
            # terminate the live worker runs and clear the zombie state.
            if mission_status in {"cancelled", "canceled"}:
                _mark_conversation_mission_cancelled()
            self.update_session_index_for_mission(
                mission_id, status="idle", running=False, waiting_approval=False,
            )
            self._append_team_mission_state_projection_event(
                mission_id=mission_id,
                event={
                    "type": "mission.status.projected",
                    "timestamp": time.time(),
                    "payload": {
                        "mission_status": mission_status or "cancelled",
                        "reason": "already_terminal_cancel_observed",
                    },
                },
                identity={"mission_id": mission_id, "missionId": mission_id},
                dedupe_key=f"mission-status-projected:{mission_id}:{mission_status or 'terminal'}",
            )
            return {
                "mission_id": mission_id,
                "mission_status": mission_status or "cancelled",
                "canceled_nodes": [],
                "cancel_run_bindings": cancel_run_bindings,
                "graph": self.get_team_mission_graph(mission_id),
            }
        cancellation_metadata = {
            "canceled_by": _text(canceled_by),
            "cancel_reason": _text(reason),
            "canceled_at": canceled_at,
        }
        canceled_nodes = []
        canceled_node_ids: set[str] = set()
        for node in nodes:
            node_id = _text(node.get("node_id"))
            node_status = _text(node.get("status")).lower()
            if not node_id or node_status in _TERMINAL_NODE_STATUSES:
                continue
            if node_status not in _CANCELLABLE_NODE_STATUSES and node_status:
                continue
            canceled_node_ids.add(node_id)
            canceled_nodes.append(self.upsert_team_mission_node(
                mission_id=mission_id,
                node_id=node_id,
                kind=str(node.get("kind") or "worker"),
                title=str(node.get("title") or ""),
                objective=str(node.get("objective") or ""),
                status="cancelled",
                assignee_profile_id=str(node.get("assignee_profile_id") or ""),
                assignee_profile_version_id=str(node.get("assignee_profile_version_id") or ""),
                runtime_scope_key=str(node.get("runtime_scope_key") or ""),
                output_contract=dict(node.get("output_contract") or {}),
                metadata={
                    **dict(node.get("metadata") or {}),
                    **cancellation_metadata,
                },
                position_x=float(node.get("position_x") or 0),
                position_y=float(node.get("position_y") or 0),
            ))
        for binding in bindings:
            run_id = _text(binding.get("run_id"))
            node_id = _text(binding.get("node_id"))
            if not run_id:
                continue
            if run_id in active_run_ids or node_id in canceled_node_ids:
                if not any(_text(item.get("run_id")) == run_id for item in cancel_run_bindings):
                    cancel_run_bindings.append(binding)
                if run_id not in active_run_ids and hasattr(self, "get_run") and hasattr(self, "upsert_run"):
                    # Reap runs surfaced only via a cancelled node (not seen in
                    # the first status sweep) so they cannot stay non-terminal.
                    run = self.get_run(run_id)
                    if run and _text(run.get("status")).lower() not in _TERMINAL_RUN_STATUSES:
                        active_run_ids.add(run_id)
                        self.upsert_run(
                            run_id=run_id,
                            session_id=_text(run.get("session_id")) or _text(binding.get("session_id")),
                            runtime_scope_key=_text(run.get("runtime_scope_key")) or _text(binding.get("runtime_scope_key")),
                            turn_id=_text(run.get("turn_id")),
                            runtime_session_id=_text(run.get("runtime_session_id")) or _text(binding.get("runtime_session_id")),
                            status="cancelled",
                            completed_at=canceled_at,
                            metadata={
                                "cancelled_by": _text(canceled_by) or "team_mission.cancel",
                                "cancel_reason": _text(reason),
                                "cancelled_mission_id": mission_id,
                            },
                        )
        self.upsert_team_mission(
            mission_id=mission_id,
            team_id=str(mission.get("team_id") or ""),
            conversation_id=str(mission.get("conversation_id") or ""),
            title=str(mission.get("title") or ""),
            objective=str(mission.get("objective") or ""),
            workspace_id=str(mission.get("workspace_id") or ""),
            workspace_path=str(mission.get("workspace_path") or ""),
            mode=str(mission.get("mode") or ""),
            status="cancelled",
            leader_session_id=str(mission.get("leader_session_id") or ""),
            metadata={
                **dict(mission.get("metadata") or {}),
                **cancellation_metadata,
            },
        )
        _mark_conversation_mission_cancelled()
        # Cancel does NOT go through reduce_team_mission_graph, so project the now-
        # terminal status onto the conversation's session_index here — otherwise the
        # sidebar keeps showing the cancelled mission as "running" after restart.
        self.update_session_index_for_mission(
            mission_id, status="idle", running=False, waiting_approval=False,
        )
        self._append_team_mission_state_projection_event(
            mission_id=mission_id,
            event={
                "type": "mission.cancelled",
                "timestamp": canceled_at,
                "payload": {
                    "mission_status": "cancelled",
                    "node_ids": [str(node.get("node_id") or "") for node in canceled_nodes],
                    "canceled_by": _text(canceled_by),
                    "reason": _text(reason),
                },
            },
            identity={"mission_id": mission_id, "missionId": mission_id},
            dedupe_key=f"mission-cancelled:{mission_id}",
        )
        _log.debug(
            "[doxie-cancel] cancel_team_mission DONE mission_id=%s canceled_node_count=%s canceled_run_count=%s "
            "event_emit=YES",
            mission_id, len(canceled_nodes), len(cancel_run_bindings),
        )
        return {
            "mission_id": mission_id,
            "mission_status": "cancelled",
            "canceled_nodes": canceled_nodes,
            "cancel_run_bindings": cancel_run_bindings,
            "graph": self.get_team_mission_graph(mission_id),
        }

    def reap_terminal_mission_runs(
        self,
        mission_id: str = "",
        *,
        stale_after_seconds: float = DEFAULT_ORPHANED_ACTIVE_RUN_STALE_SECONDS,
        owner_dead_grace_seconds: float = DEFAULT_ORPHANED_ACTIVE_RUN_OWNER_DEAD_GRACE_SECONDS,
    ) -> int:
        """Reap only run-level orphans.

        Mission-bound runs are reaped only when their own mission is terminal.
        Legacy/direct runs without a mission binding use the normal active-run
        orphan stale decision, and only when their conversation has no active
        mission. The ``mission_id`` argument is a trigger hint retained for
        existing callers; the decision is per run.
        """
        trigger_mission_id = _text(mission_id)
        if not hasattr(self, "upsert_run"):
            return 0
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    r.*,
                    b.mission_id AS binding_mission_id,
                    b.session_id AS binding_session_id,
                    b.runtime_scope_key AS binding_runtime_scope_key,
                    b.runtime_session_id AS binding_runtime_session_id,
                    b.metadata_json AS binding_metadata_json,
                    m.status AS mission_status,
                    m.conversation_id AS mission_conversation_id
                FROM runs r
                LEFT JOIN team_mission_run_bindings b ON b.run_id = r.run_id
                LEFT JOIN team_missions m ON m.mission_id = b.mission_id
                WHERE r.status = 'running'
                ORDER BY r.updated_at DESC, r.started_at DESC
                """,
            ).fetchall()

        def _mission_row_for_run(run_mission_id: str) -> sqlite3.Row | None:
            if not run_mission_id:
                return None
            with self._lock:
                return self._conn.execute(
                    "SELECT status, conversation_id FROM team_missions WHERE mission_id = ?",
                    (run_mission_id,),
                ).fetchone()

        def _conversation_id_for_legacy_run(run: dict[str, Any]) -> str:
            metadata = run.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            conversation_id = _text(metadata.get("conversation_id") or metadata.get("conversationId"))
            if conversation_id:
                return conversation_id
            session_id = _text(run.get("session_id"))
            if not session_id:
                return ""
            with self._lock:
                row = self._conn.execute(
                    """
                    SELECT conversation_id
                    FROM team_mission_conversations
                    WHERE stable_session_id = ? OR conversation_id = ?
                    LIMIT 1
                    """,
                    (session_id, session_id),
                ).fetchone()
            return _text(_row_value(row, "conversation_id", "")) or session_id

        reaped = 0
        for row in rows:
            run_id = _text(_row_value(row, "run_id", ""))
            if not run_id:
                continue
            run_metadata = _json_loads(_row_value(row, "metadata_json", ""), {})
            if not isinstance(run_metadata, dict):
                run_metadata = {}
            run = dict(row)
            run["metadata"] = run_metadata
            if _text(run.get("status")).lower() != "running":
                continue
            run_mission_id = (
                _text(_row_value(row, "binding_mission_id", ""))
                or _text(run_metadata.get("mission_id") or run_metadata.get("missionId"))
            )
            mission_status = _text(_row_value(row, "mission_status", "")).lower()
            if run_mission_id:
                if not mission_status:
                    mission_row = _mission_row_for_run(run_mission_id)
                    mission_status = _text(_row_value(mission_row, "status", "")).lower()
                if mission_status not in _TERMINAL_MISSION_STATUSES:
                    continue
                reap_reason = "terminal_mission_stale_run"
                error = f"runtime run reaped: bound team mission {run_mission_id} already terminal"
            else:
                conversation_id = _conversation_id_for_legacy_run(run)
                if conversation_id and self.has_active_mission(conversation_id):
                    continue
                should_reap, stale_decision = orphaned_active_run_decision(
                    row,
                    now=now,
                    live_runtime_session_ids=set(),
                    stale_after_seconds=stale_after_seconds,
                    owner_dead_grace_seconds=owner_dead_grace_seconds,
                )
                if not should_reap:
                    continue
                reap_reason = f"legacy_run_without_mission:{stale_decision}"
                error = "runtime run reaped: no active mission owns stale legacy run"
            self.upsert_run(
                run_id=run_id,
                session_id=_text(run.get("session_id")) or _text(_row_value(row, "binding_session_id", "")),
                runtime_scope_key=_text(run.get("runtime_scope_key")) or _text(_row_value(row, "binding_runtime_scope_key", "")),
                turn_id=_text(run.get("turn_id")),
                runtime_session_id=_text(run.get("runtime_session_id")) or _text(_row_value(row, "binding_runtime_session_id", "")),
                status="interrupted",
                completed_at=now,
                error=error,
                metadata={
                    **dict(run_metadata),
                    "reaped_reason": reap_reason,
                    **({"reaped_mission_id": run_mission_id} if run_mission_id else {}),
                    **({"reaper_trigger_mission_id": trigger_mission_id} if trigger_mission_id else {}),
                },
            )
            reaped += 1
        return reaped

    def prune_team_mission_events(self, mission_id: str) -> int:
        """Drop high-volume streaming delta rows for an already-terminal mission.

        team_mission_events had no retention (unlike run_events), so every
        streamed token delta accumulated forever — the canonical log grew into
        the GBs, which slowed every query and made the concurrent session-list
        loads time out (manifesting as 'lost' running state). Once a mission is
        terminal its per-token deltas are no longer needed: the final text lives
        in the kept message.complete events, and structure/tool boundaries are
        kept too, so reopening a finished mission still renders its result. Only
        prunes terminal missions; active missions are never touched. Idempotent;
        freed pages are reused so growth is capped without a VACUUM."""
        mission_id = _text(mission_id)
        if not mission_id:
            return 0
        with self._lock:
            mission_row = self._conn.execute(
                "SELECT status FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
            if mission_row is None:
                return 0
            if _text(_row_value(mission_row, "status", "")).lower() not in _TERMINAL_MISSION_STATUSES:
                return 0
        placeholders = ",".join("?" for _ in _TEAM_MISSION_PRUNABLE_SOURCE_TYPES)

        def _do(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                f"""
                DELETE FROM team_mission_events
                WHERE mission_id = ?
                  AND source_event_type IN ({placeholders})
                """,
                (mission_id, *_TEAM_MISSION_PRUNABLE_SOURCE_TYPES),
            )
            return int(cursor.rowcount or 0)

        return self._execute_write(_do)

    def _prune_team_mission_events_if_terminal(self, mission_id: str) -> int:
        try:
            return self.prune_team_mission_events(mission_id)
        except Exception as exc:
            _log.debug("team mission stream pruning skipped for %s: %s", mission_id, exc)
            return 0

    def bind_team_mission_run(
        self,
        *,
        mission_id: str,
        node_id: str,
        run_id: str,
        session_id: str,
        role: str = "worker",
        runtime_session_id: str = "",
        runtime_scope_key: str = "",
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        run_id = str(run_id or "").strip()
        session_id = str(session_id or "").strip()
        if not mission_id or not run_id or not session_id:
            return {}
        now = time.time()

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            existing = conn.execute(
                "SELECT metadata_json, created_at FROM team_mission_run_bindings WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            merged_metadata = _json_loads(_row_value(existing, "metadata_json", ""), {})
            if isinstance(metadata, dict):
                merged_metadata.update(metadata)
            conn.execute(
                """
                INSERT INTO team_mission_run_bindings (
                    mission_id, node_id, run_id, session_id, runtime_session_id,
                    runtime_scope_key, role, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    mission_id = excluded.mission_id,
                    node_id = excluded.node_id,
                    session_id = excluded.session_id,
                    runtime_session_id = excluded.runtime_session_id,
                    runtime_scope_key = excluded.runtime_scope_key,
                    role = excluded.role,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    mission_id,
                    str(node_id or ""),
                    run_id,
                    session_id,
                    str(runtime_session_id or ""),
                    str(runtime_scope_key or ""),
                    str(role or "worker"),
                    _json_dumps(merged_metadata if isinstance(merged_metadata, dict) else {}),
                    float(_row_value(existing, "created_at", now) or now),
                    now,
                ),
            )
            if str(node_id or "").strip():
                canonical_node_id = _conversation_graph_node_id(mission_id, str(node_id or ""))
                task_frame_id = f"mission-frame:{mission_id}" if mission_id else ""
                conn.execute(
                    """
                    UPDATE team_mission_nodes
                       SET canonical_node_id = COALESCE(NULLIF(canonical_node_id, ''), ?),
                           task_frame_id = COALESCE(NULLIF(task_frame_id, ''), ?),
                           runtime_stable_session_id = COALESCE(NULLIF(?, ''), runtime_stable_session_id),
                           runtime_session_id = COALESCE(NULLIF(?, ''), runtime_session_id),
                           runtime_scope_key = COALESCE(NULLIF(?, ''), runtime_scope_key),
                           updated_at = ?
                     WHERE mission_id = ?
                       AND node_id = ?
                    """,
                    (
                        canonical_node_id,
                        task_frame_id,
                        session_id,
                        str(runtime_session_id or ""),
                        str(runtime_scope_key or ""),
                        now,
                        mission_id,
                        str(node_id or ""),
                    ),
                )
            return self._team_mission_run_binding_from_row(conn.execute(
                "SELECT * FROM team_mission_run_bindings WHERE run_id = ?",
                (run_id,),
            ).fetchone()) or {}

        return self._execute_write(_do)

    def get_team_mission_run_binding(self, run_id: str) -> Dict[str, Any]:
        run_id = str(run_id or "").strip()
        if not run_id:
            return {}
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM team_mission_run_bindings WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self._team_mission_run_binding_from_row(row) or {}

    def team_mission_run_session_ids(self, session_ids: list[str]) -> set[str]:
        normalized = [str(session_id or "").strip() for session_id in session_ids]
        normalized = [session_id for session_id in normalized if session_id]
        if not normalized:
            return set()
        placeholders = ",".join("?" for _ in normalized)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT DISTINCT session_id, runtime_session_id
                FROM team_mission_run_bindings
                WHERE session_id IN ({placeholders})
                   OR runtime_session_id IN ({placeholders})
                """,
                tuple(normalized + normalized),
            ).fetchall()
        requested = set(normalized)
        internal_ids: set[str] = set()
        for row in rows:
            for key in ("session_id", "runtime_session_id"):
                value = str(_row_value(row, key, "") or "").strip()
                if value and value in requested:
                    internal_ids.add(value)
        return internal_ids

    def is_team_mission_run_session(self, session_id: str) -> bool:
        session_id = str(session_id or "").strip()
        return bool(session_id and session_id in self.team_mission_run_session_ids([session_id]))

    def _team_mission_run_has_deliverable_text(self, run_id: str, *, max_seq: int = 0) -> bool:
        run_id = str(run_id or "").strip()
        if not run_id:
            return False
        sql = "SELECT event_type, payload_json, event_json, seq FROM run_events WHERE run_id = ?"
        params: list[Any] = [run_id]
        if max_seq > 0:
            sql += " AND seq <= ?"
            params.append(max_seq)
        sql += " ORDER BY seq ASC, id ASC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        for row in rows:
            event_type = _text(_row_value(row, "event_type"))
            payload = _json_loads(_row_value(row, "payload_json", ""), None)
            if not isinstance(payload, dict):
                event = _json_loads(_row_value(row, "event_json", ""), {})
                payload = event.get("payload") if isinstance(event, dict) and isinstance(event.get("payload"), dict) else {}
            if _event_has_deliverable_text(event_type, payload):
                return True
        return False
