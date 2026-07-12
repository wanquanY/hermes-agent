from __future__ import annotations

# ruff: noqa: F401,F403,F405
from .session_common import *
from hermes_team_mission.domain.runtime_identity import runtime_event_identity


class TeamMissionEventMixin:
    def _record_missing_team_mission_handoff(
        self,
        *,
        binding: Dict[str, Any],
        node: Dict[str, Any],
        run_id: str,
        event: Dict[str, Any],
    ) -> Dict[str, Any]:
        mission_id = _text(binding.get("mission_id"))
        node_id = _text(binding.get("node_id"))
        if not mission_id or not node_id or not run_id:
            return {}
        node_metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        binding_metadata = binding.get("metadata") if isinstance(binding.get("metadata"), dict) else {}
        task_id = _text(
            node_metadata.get("task_id")
            or node_metadata.get("taskId")
            or binding_metadata.get("task_id")
            or binding_metadata.get("taskId")
        )
        payload = {
            "reason_code": "protocol_violation",
            "recoverability": "blocked",
            "required_tool": "team_mission_submit_deliverable",
            "source_event_type": _text((event or {}).get("type")),
            "source_event_seq": _event_seq(event),
            "message": "Team Mission node ended without a required structured handoff, and no recoverable structured final text was found.",
        }
        return self.upsert_team_mission_deliverable(
            mission_id=mission_id,
            node_id=node_id,
            run_id=run_id,
            task_id=task_id,
            status="blocked",
            result="MISSING",
            summary=payload["message"],
            payload=payload,
            artifact_refs=[],
            next_context={},
            output_contract=dict(node.get("output_contract") or {}),
            source=_deliverable_state.DELIVERABLE_SOURCE_MISSING,
            confidence=0.0,
            visibility=_deliverable_state.DELIVERABLE_VISIBILITY_HANDOFF,
        )

    def reduce_team_mission_run_event(self, *, run_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
        run_id = str(run_id or "").strip()
        if not run_id:
            return {}
        binding = self.get_team_mission_run_binding(run_id)
        if not binding:
            return {}
        event_type = str((event or {}).get("type") or "").strip()
        payload = event.get("payload") if isinstance((event or {}).get("payload"), dict) else {}
        next_status = ""
        if event_type == "error":
            next_status = "failed"
        elif event_type == "message.complete":
            status = str(payload.get("status") or "").strip().lower()
            if status in {"cancelled", "canceled"}:
                next_status = "cancelled"
            elif status == "interrupted":
                next_status = "interrupted"
            elif status in {"failed", "error"}:
                # A failed run's streamed prose is not an authoritative
                # deliverable. Only the explicit handoff contract can turn an
                # error terminal into a completed node.
                if self.team_mission_run_has_deliverable(run_id):
                    next_status = "completed"
                else:
                    next_status = "failed"
            else:
                next_status = "completed"
        if not next_status:
            return {}
        node = self.get_team_mission_node(str(binding.get("mission_id") or ""), str(binding.get("node_id") or ""))
        if not node:
            return {}
        missing_required_handoff = False
        missing_handoff_failure: Dict[str, str] = {}
        if (
            next_status == "completed"
            and _node_requires_explicit_handoff(node)
            and not self.team_mission_run_has_deliverable(run_id)
        ):
            self._record_missing_team_mission_handoff(
                binding=binding,
                node=node,
                run_id=run_id,
                event=event,
            )
            next_status = "blocked"
            missing_required_handoff = True
            missing_handoff_failure = _classify_team_mission_failure(
                "error",
                {"error": "protocol violation: Team Mission node ended without calling team_mission_submit_deliverable"},
            )
        metadata = dict(node.get("metadata") or {})
        failure = _classify_team_mission_failure(event_type, payload)
        event_seq = _event_seq(event)
        existing_terminal_run_id = str(metadata.get("last_run_id") or "").strip()
        existing_terminal_status = str(
            metadata.get("last_run_terminal_status")
            or node.get("status")
            or ""
        ).strip().lower()
        try:
            existing_terminal_seq = int(metadata.get("last_run_terminal_seq") or 0)
        except (TypeError, ValueError):
            existing_terminal_seq = 0
        if existing_terminal_run_id == run_id and existing_terminal_status in _TERMINAL_NODE_STATUSES:
            if _text(metadata.get("last_run_terminal_event")) == "mission.node.finished":
                preferred_status = _prefer_terminal_node_status(existing_terminal_status, next_status)
                if preferred_status == existing_terminal_status:
                    return node
            if event_seq > 0 and existing_terminal_seq >= event_seq:
                return node
            preferred_status = _prefer_terminal_node_status(existing_terminal_status, next_status)
            if preferred_status != next_status:
                return node
        updated = self.upsert_team_mission_node(
            mission_id=str(binding.get("mission_id") or ""),
            node_id=str(binding.get("node_id") or ""),
            kind=str(node.get("kind") or "worker"),
            title=str(node.get("title") or ""),
            objective=str(node.get("objective") or ""),
            status=next_status,
            assignee_profile_id=str(node.get("assignee_profile_id") or ""),
            assignee_profile_version_id=str(node.get("assignee_profile_version_id") or ""),
            runtime_scope_key=str(node.get("runtime_scope_key") or binding.get("runtime_scope_key") or ""),
            output_contract=dict(node.get("output_contract") or {}),
            metadata={
                **metadata,
                "last_run_id": run_id,
                "last_run_terminal_event": event_type,
                "last_run_terminal_status": next_status,
                "last_run_terminal_seq": event_seq,
                **({
                    "last_run_reason_code": missing_handoff_failure.get("reason_code") or "protocol_violation",
                    "last_run_recoverability": missing_handoff_failure.get("recoverability") or "blocked",
                    "last_run_error_message": missing_handoff_failure.get("message") or "Team Mission node ended without submitting required handoff deliverable.",
                } if missing_required_handoff else {}),
                **({
                    "last_run_reason_code": failure["reason_code"],
                    "last_run_recoverability": failure["recoverability"],
                    "last_run_error_message": failure["message"],
                } if failure and not missing_required_handoff else {}),
            },
            position_x=float(node.get("position_x") or 0),
            position_y=float(node.get("position_y") or 0),
        )
        if missing_required_handoff:
            try:
                _event_log.append_team_mission_structural_event(
                    self,
                    mission_id=str(binding.get("mission_id") or ""),
                    source_event={
                        "type": "mission.node.blocked",
                        "run_id": run_id,
                        "seq": event_seq,
                        "payload": {
                            "node": updated,
                            "run_id": run_id,
                            "reason_code": missing_handoff_failure.get("reason_code") or "protocol_violation",
                            "recoverability": missing_handoff_failure.get("recoverability") or "blocked",
                            "message": missing_handoff_failure.get("message") or "Team Mission node ended without submitting required handoff deliverable.",
                        },
                    },
                    dedupe_key=f"node-blocked:missing-handoff:{binding.get('mission_id') or ''}:{binding.get('node_id') or ''}:{run_id}",
                )
            except Exception:
                pass
        self.reduce_team_mission_graph(str(binding.get("mission_id") or ""))
        compile_mode = "final"
        if next_status in {"cancelled", "interrupted"}:
            compile_mode = "canceled"
        elif next_status in {"failed", "blocked"}:
            compile_mode = "blocked"
        try:
            self.compile_team_mission_memory(
                mission_id=str(binding.get("mission_id") or ""),
                mode=compile_mode,
                source_run_ids=[run_id],
                emit_event=True,
            )
        except Exception:
            pass
        # Planning lifecycle backstop: a leader planning run that ends WITHOUT calling
        # team_mission_plan_complete (e.g. the model wrote the clarification or the
        # plan as prose and stopped instead of using the clarify/plan_complete tools)
        # would otherwise leave the mission stuck in running/planning with nothing to
        # approve or execute — the conversation list and input box stay "running"
        # forever. If a leader planning node has just terminated and there are still
        # no worker/verifier/synthesis/approval nodes, fail the mission so the UI
        # unblocks. A live clarify keeps the run blocked waiting on the user, so the
        # terminal event never fires and this branch does not run — clarify flows
        # are safe.
        try:
            mission_id_for_check = str(binding.get("mission_id") or "")
            if (
                mission_id_for_check
                and _normalize_node_kind(node.get("kind")) == "root"
                and next_status in {"completed", "failed", "cancelled", "interrupted"}
            ):
                graph_for_check = self.team_mission_graphs.get_team_mission_graph(mission_id_for_check)
                mission_for_check = graph_for_check.get("mission") or {}
                mission_status = _text(mission_for_check.get("status")).lower()
                if not _is_terminal_mission_status(mission_status) and mission_status != "waiting_approval":
                    has_real_node = any(
                        _normalize_node_kind(_n.get("kind")) not in {"root", ""}
                        for _n in (graph_for_check.get("nodes") or [])
                        if isinstance(_n, dict)
                    )
                    if not has_real_node:
                        self.upsert_team_mission(
                            mission_id=mission_id_for_check,
                            team_id=_text(mission_for_check.get("team_id")),
                            title=_text(mission_for_check.get("title")),
                            mode=_text(mission_for_check.get("mode")) or "supervised_mission",
                            status="failed",
                            leader_session_id=_text(mission_for_check.get("leader_session_id")),
                        )
        except Exception:
            pass
        return updated

    def append_team_mission_run_event(
        self,
        *,
        mission_id: str,
        run_id: str,
        event: Dict[str, Any],
    ) -> Dict[str, Any]:
        mission_id = str(mission_id or "").strip()
        run_id = str(run_id or "").strip()
        if not mission_id or not run_id:
            return {}
        with self._lock:
            binding = self._conn.execute(
                "SELECT * FROM team_mission_run_bindings WHERE mission_id = ? AND run_id = ?",
                (mission_id, run_id),
            ).fetchone()
            mission_row = self._conn.execute(
                "SELECT * FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
            node_row = self._conn.execute(
                "SELECT * FROM team_mission_nodes WHERE mission_id = ? AND node_id = ?",
                (mission_id, _row_value(binding, "node_id", "") if binding is not None else ""),
            ).fetchone() if binding is not None else None
        if binding is None:
            return {}
        mission = self.team_mission_rows.mission_from_row(mission_row) or {"mission_id": mission_id}
        node = self.team_mission_rows.node_from_row(node_row) or {}
        binding_value = self.team_mission_rows.run_binding_from_row(binding) or {}
        identity = runtime_event_identity(
            mission=mission,
            node=node,
            binding=binding_value,
        )
        frame = dict(event or {})
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        runtime_conversation_session_id = str(binding["session_id"] or "").strip()
        frame.update({
            "run_id": run_id,
            "session_id": str(frame.get("session_id") or binding["execution_session_id"] or ""),
            "conversation_session_id": runtime_conversation_session_id,
            "runtime_scope_key": str(frame.get("runtime_scope_key") or binding["runtime_scope_key"] or binding["session_id"] or ""),
            "payload": payload,
        })
        activity_id = f"mission:{mission_id}"
        frame["activity_id"] = activity_id
        frame["activityId"] = activity_id
        payload["activity_id"] = activity_id
        payload["activityId"] = activity_id
        frame = _runtime_event_with_team_mission_identity(frame, identity)
        prev_projecting = getattr(self, "_team_mission_projecting", False)
        # Guard so the inner append_run_event (and any conversation mirror it
        # triggers) does not re-enter the write-time projection hook; this
        # explicit path performs the canonical projection itself below.
        self._team_mission_projecting = True
        try:
            saved = self.runs.append_event(str(binding["session_id"] or ""), frame)
            if (
                isinstance(saved, dict)
                and saved.get("_persistence_disposition") in {"duplicate_terminal", "ignored_after_terminal"}
            ):
                return saved
            source_event = dict(frame)
            if isinstance(saved, dict):
                for key in ("timestamp", "session_id", "conversation_session_id", "runtime_scope_key", "execution_session_id"):
                    if saved.get(key) is not None and not source_event.get(key):
                        source_event[key] = saved.get(key)
            self._project_team_mission_run_event_locked(
                mission_id=mission_id,
                run_id=run_id,
                binding=binding_value,
                identity=identity,
                node=node,
                source_event=source_event,
            )
            terminal_status = _terminal_run_status_for_event(
                _text(source_event.get("type")),
                source_event.get("payload") if isinstance(source_event.get("payload"), dict) else {},
            )
            if terminal_status:
                self.runs.retention.maintain_after_append(
                    session_id=str(binding["session_id"] or ""),
                    run_id=run_id,
                    seq=_event_seq(source_event),
                    terminal_status=terminal_status,
                )
                self._prune_team_mission_events_if_terminal(mission_id)
            return saved
        finally:
            self._team_mission_projecting = prev_projecting

    def _project_team_mission_run_event_locked(
        self,
        *,
        mission_id: str,
        run_id: str,
        binding: Dict[str, Any],
        identity: Dict[str, str],
        node: Dict[str, Any],
        source_event: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Canonical projection for one runtime event of a team-mission-bound run.

        Single implementation shared by both the explicit
        ``append_team_mission_run_event`` path and the write-time hook in
        ``append_run_event`` (directly-delivered node events).
        """
        mission_event = _event_log.append_team_mission_runtime_event(
            self,
            mission_id=mission_id,
            run_id=run_id,
            source_event=source_event,
            identity=identity,
        )
        self.reduce_team_mission_run_event(run_id=run_id, event=source_event)
        if (
            isinstance(mission_event, dict)
            and not mission_event.get("_persistence_disposition")
            and _should_emit_conversation_status_projection(source_event, node=node)
        ):
            _event_log.append_team_mission_conversation_status_event(
                self,
                mission_id=mission_id,
                source_event=source_event,
                source_mission_seq=_event_seq(mission_event),
            )
        return mission_event

    def _project_team_mission_run_event(self, *, run_id: str, saved: Dict[str, Any]) -> None:
        """Write-time canonical projection for directly-delivered run events.

        Invoked from ``append_run_event`` for runtime events recorded straight
        onto a node's session (the streaming path that does not go through
        ``append_team_mission_run_event``). Replaces the removed read-time
        ``run_events`` -> mission projection so replay/live share one seq domain.
        """
        run_id = str(run_id or "").strip()
        if not run_id or not isinstance(saved, dict):
            return
        if saved.get("_persistence_disposition") in {
            "duplicate_terminal",
            "ignored_after_terminal",
            "duplicate_mission_event",
        }:
            return
        binding = self.get_team_mission_run_binding(run_id)
        if not binding:
            return
        mission_id = str(binding.get("mission_id") or "").strip()
        if not mission_id:
            return
        node = self.get_team_mission_node(mission_id, str(binding.get("node_id") or "")) or {}
        with self._lock:
            mission_row = self._conn.execute(
                "SELECT * FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
        mission = self.team_mission_rows.mission_from_row(mission_row) or {"mission_id": mission_id}
        identity = runtime_event_identity(
            mission=mission,
            node=node,
            binding=binding,
        )
        prev_projecting = getattr(self, "_team_mission_projecting", False)
        self._team_mission_projecting = True
        try:
            # Directly-delivered runtime events only need to exist in the
            # canonical log so replay/live share one seq domain (INV-1). Node
            # status reduction, conversation mirroring and status projection are
            # owned by the explicit streaming paths (append_team_mission_run_event
            # and run_control.record_event); doing them here would double-mirror
            # and rewrite conversation stream history.
            source_event = dict(saved)
            _event_log.append_team_mission_runtime_event(
                self,
                mission_id=mission_id,
                run_id=run_id,
                source_event=source_event,
                identity=identity,
            )
            payload = source_event.get("payload") if isinstance(source_event.get("payload"), dict) else {}
            if _terminal_run_status_for_event(_text(source_event.get("type")), payload):
                self._prune_team_mission_events_if_terminal(mission_id)
        finally:
            self._team_mission_projecting = prev_projecting
