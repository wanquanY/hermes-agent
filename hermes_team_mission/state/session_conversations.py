from __future__ import annotations

# ruff: noqa: F401,F403,F405
from .session_common import *
from hermes_state_participants import leader_participant_id, member_participant_id


class SessionDBTeamMissionConversationMixin:
    def upsert_team_mission_conversation(
        self,
        *,
        conversation_id: str,
        team_id: str = "",
        stable_session_id: str = "",
        title: str = "",
        objective: str = "",
        workspace_id: str = "",
        workspace_path: str = "",
        status: str = "active",
        active_mission_id: str = "",
        created_by_user_id: str = "",
        created_at: float | None = None,
        updated_at: float | None = None,
        metadata: Dict[str, Any] | None = None,
        replace_title: bool = False,
        touch: bool = False,
    ) -> Dict[str, Any]:
        conversation_id = _text(conversation_id)
        if not conversation_id:
            return {}
        stable_session_id = _text(stable_session_id) or conversation_id
        now = time.time()
        created = float(created_at or now)
        requested_updated = float(updated_at if updated_at is not None else now)

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            existing = conn.execute(
                "SELECT metadata_json, title, created_at, updated_at FROM team_mission_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            existing_metadata = _json_loads(_row_value(existing, "metadata_json", ""), {})
            merged_metadata = dict(existing_metadata)
            if isinstance(metadata, dict):
                merged_metadata.update(metadata)
            merged_metadata["conversation_id"] = conversation_id
            merged_metadata["stable_session_id"] = stable_session_id
            existing_title = _text(_row_value(existing, "title", ""))
            requested_title = _text(title)
            existing_display_title_source = _text(
                existing_metadata.get("display_title_source")
                or existing_metadata.get("displayTitleSource")
            )
            requested_display_title_source = _text(
                merged_metadata.get("display_title_source")
                or merged_metadata.get("displayTitleSource")
            )
            existing_title_is_replaceable = _is_replaceable_team_mission_conversation_title(
                existing_title,
                existing_display_title_source,
            )
            should_write_requested_title = bool(
                requested_title
                and (replace_title or not existing_title or existing_title_is_replaceable)
            )
            if (
                should_write_requested_title
                and not _is_placeholder_team_mission_conversation_title(requested_title)
                and not requested_display_title_source
            ):
                merged_metadata["display_title_source"] = "first_user_message"
            insert_title = requested_title or existing_title or "Team Mission"
            update_title = requested_title if should_write_requested_title else ""
            existing_updated = float(_row_value(existing, "updated_at", requested_updated) or requested_updated)
            update_updated = requested_updated if (updated_at is not None or touch or existing is None) else existing_updated
            conn.execute(
                """
                INSERT INTO team_mission_conversations (
                    conversation_id, team_id, stable_session_id, title, objective,
                    workspace_id, workspace_path, status, active_mission_id,
                    created_by_user_id, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                    team_id = COALESCE(NULLIF(excluded.team_id, ''), team_id),
                    stable_session_id = excluded.stable_session_id,
                    title = COALESCE(NULLIF(?, ''), title),
                    objective = COALESCE(NULLIF(excluded.objective, ''), objective),
                    workspace_id = COALESCE(NULLIF(excluded.workspace_id, ''), workspace_id),
                    workspace_path = COALESCE(NULLIF(excluded.workspace_path, ''), workspace_path),
                    status = excluded.status,
                    active_mission_id = COALESCE(NULLIF(excluded.active_mission_id, ''), active_mission_id),
                    created_by_user_id = COALESCE(NULLIF(excluded.created_by_user_id, ''), created_by_user_id),
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    conversation_id,
                    _text(team_id),
                    stable_session_id,
                    insert_title,
                    _text(objective),
                    _text(workspace_id),
                    _text(workspace_path),
                    _conversation_status(status),
                    _text(active_mission_id),
                    _text(created_by_user_id),
                    _json_dumps(merged_metadata if isinstance(merged_metadata, dict) else {}),
                    float(_row_value(existing, "created_at", created) or created),
                    update_updated,
                    update_title,
                ),
            )
            if _text(active_mission_id):
                self._add_mission_to_conversation_on_conn(
                    conn,
                    conversation_id=conversation_id,
                    mission_id=_text(active_mission_id),
                    status="active",
                    now=update_updated,
                )
            return self._team_mission_conversation_from_row(conn.execute(
                "SELECT * FROM team_mission_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()) or {}

        record = self._execute_write(_do)
        self._project_team_conversation_to_session_index(record)
        return record

    def _project_team_conversation_to_session_index(self, record: Dict[str, Any]) -> None:
        """Surface a team-mission conversation in the control-plane session_index.

        ON CONFLICT updates static/team fields plus the conversation-level running
        boolean, whose source is conversation_missions any-active. Detailed live
        status/run fields remain owned by update_session_index_for_mission. Keyed
        by the conversation's stable session id; carries mission_id so mission
        status updates can target it. Best-effort."""
        if not record:
            return
        sid = _text(record.get("stable_session_id")) or _text(record.get("conversation_id"))
        if not sid:
            return
        title = _text(record.get("title"))
        team_id = _text(record.get("team_id"))
        conversation_id = _text(record.get("conversation_id"))
        mission_id = _text(record.get("active_mission_id"))
        running = self.has_active_mission(conversation_id)
        message_count = int(record.get("message_count") or 0)
        started = float(record.get("created_at") or 0)
        updated = float(record.get("updated_at") or 0) or started

        def _do(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO session_index (
                    session_id, title, source, session_kind, team_id,
                    conversation_id, mission_id, running, message_count, started_at, updated_at
                ) VALUES (?, ?, 'team_mission', 'team_mission', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    title=excluded.title,
                    source=excluded.source,
                    session_kind=excluded.session_kind,
                    team_id=excluded.team_id,
                    conversation_id=excluded.conversation_id,
                    mission_id=excluded.mission_id,
                    running=excluded.running,
                    message_count=excluded.message_count,
                    updated_at=excluded.updated_at
                """,
                (
                    sid,
                    title,
                    team_id,
                    conversation_id,
                    mission_id,
                    1 if running else 0,
                    message_count,
                    started,
                    updated,
                ),
            )

        try:
            self._execute_write(_do)
        except Exception:
            pass

    def update_session_index_pending_state_for_session_key(
        self,
        session_key: str,
        *,
        waiting_approval: bool,
    ) -> int:
        """Project an in-process tool-approval / clarify state-change onto
        every session_index row that this ``session_key`` belongs to.

        The single resolver that lets ``waiting_approval`` cover all
        conversation types — normal, team-leader, team-member-node — using
        five complementary match paths (any/all may hit at once; we de-dupe
        target rows by session_id):

        1. Direct ``session_id == session_key`` (normal conversations whose
           agent session key IS the conversation's session id).
        2. ``runtime_scope_key == session_key`` (any agent that stamped its
           scope into the index row).
        3. ``team_mission_run_bindings.session_id / runtime_session_id``
           JOIN onto the row matching the binding's ``mission_id`` (member
           node runtime scopes).
        4. ``team_mission_conversations.stable_session_id`` JOIN onto the
           row matching the conversation's stable session id (team conv
           via stable id).
        5. Parse the ``team:<conv_id>:leader-conversation`` scope-key
           pattern and match by ``session_index.conversation_id`` (team
           leader's conversation-scoped scope key).

        UPDATE policy:
        - ``waiting_approval=True``  → ``waiting_approval=1, running=0``
          (mutually exclusive; waiting wins over running).
        - ``waiting_approval=False`` → ``waiting_approval=0``. We do NOT
          force ``running=0`` because the run may legitimately still be
          executing — let the next ``project_run`` event re-establish the
          true ``running`` flag. If a non-empty ``active_run_id`` is still
          on the row, we flip ``running=1`` directly so the sidebar
          spinner is restored immediately without waiting for the next
          run-state event.

        Returns total rows affected.
        """
        sk = _text(session_key)
        if not sk:
            return 0

        def _do(conn: sqlite3.Connection) -> int:
            target_session_ids: set[str] = set()

            def _add_session_id(value: Any) -> None:
                sid = _text(value)
                if sid:
                    target_session_ids.add(sid)

            try:
                row = conn.execute(
                    "SELECT session_id FROM session_index WHERE session_id = ?",
                    (sk,),
                ).fetchone()
                if row:
                    _add_session_id(_row_value(row, "session_id", ""))
            except Exception:
                pass

            try:
                rows = conn.execute(
                    """
                    SELECT session_id FROM session_index
                     WHERE runtime_scope_key = ? AND runtime_scope_key <> ''
                    """,
                    (sk,),
                ).fetchall()
                for row in rows or []:
                    _add_session_id(_row_value(row, "session_id", ""))
            except Exception:
                pass

            try:
                rows = conn.execute(
                    """
                    SELECT DISTINCT si.session_id
                      FROM team_mission_run_bindings tb
                      JOIN session_index si ON si.mission_id = tb.mission_id
                     WHERE tb.session_id = ?
                        OR tb.runtime_session_id = ?
                        OR tb.runtime_scope_key = ?
                    """,
                    (sk, sk, sk),
                ).fetchall()
                for row in rows or []:
                    _add_session_id(_row_value(row, "session_id", ""))
            except Exception:
                pass

            try:
                rows = conn.execute(
                    """
                    SELECT DISTINCT si.session_id
                      FROM team_mission_conversations tmc
                      JOIN session_index si
                        ON si.conversation_id = tmc.conversation_id
                     WHERE tmc.stable_session_id = ?
                    """,
                    (sk,),
                ).fetchall()
                for row in rows or []:
                    _add_session_id(_row_value(row, "session_id", ""))
            except Exception:
                pass

            # Path 5: team leader runtime-scope-key parse.
            # Pattern: ``team:<conv_id>:leader-conversation``. We extract
            # the conv_id slice between the literal head and tail and look
            # it up against the conversation_id column.
            _head = "team:"
            _tail = ":leader-conversation"
            if sk.startswith(_head) and sk.endswith(_tail):
                conv_id = sk[len(_head):-len(_tail)]
                if conv_id:
                    try:
                        rows = conn.execute(
                            """
                            SELECT session_id FROM session_index
                             WHERE conversation_id = ?
                            """,
                            (conv_id,),
                        ).fetchall()
                        for row in rows or []:
                            _add_session_id(_row_value(row, "session_id", ""))
                    except Exception:
                        pass

            if not target_session_ids:
                return 0

            total = 0
            for sid in target_session_ids:
                try:
                    if waiting_approval:
                        rc = conn.execute(
                            """
                            UPDATE session_index
                               SET waiting_approval = 1, running = 0
                             WHERE session_id = ?
                            """,
                            (sid,),
                        ).rowcount or 0
                    else:
                        rc = conn.execute(
                            """
                            UPDATE session_index
                               SET waiting_approval = 0,
                                   running = CASE
                                     WHEN active_run_id <> '' THEN 1
                                     ELSE running
                                   END
                             WHERE session_id = ?
                            """,
                            (sid,),
                        ).rowcount or 0
                    total += int(rc)
                except Exception:
                    continue
            return total

        try:
            rows = self._execute_write(_do)
        except Exception as exc:
            _log.warning(
                "[doxie-session-index] update_pending_for_session_key FAILED session_key=%s waiting=%s error=%s",
                sk, waiting_approval, exc,
            )
            return 0
        _log.debug(
            "[doxie-session-index] update_pending_for_session_key session_key=%s waiting=%s rows=%s",
            sk, waiting_approval, rows,
        )
        return rows

    def update_session_index_for_mission(
        self,
        mission_id: str,
        *,
        status: str,
        running: bool,
        waiting_approval: bool = False,
    ) -> int:
        """Project a mission's live state onto its conversation's session_index row
        (matched by mission_id). UPDATE-only; returns rows affected."""
        mid = _text(mission_id)
        if not mid:
            return 0

        def _do(conn: sqlite3.Connection) -> int:
            if not running:
                # A not-running row must NOT keep a stale active_run_id /
                # active_runtime_session_id. The sidebar derives running as
                # (running || active_run_id), so a leftover active_run_id makes a
                # finished team conversation spin forever even with running=0.
                return int(conn.execute(
                    """
                    UPDATE session_index
                       SET status = ?, running = 0, waiting_approval = ?,
                           active_run_id = '', active_runtime_session_id = '',
                           pending_approval_count = 0
                     WHERE mission_id = ?
                    """,
                    (str(status or "idle"), 1 if waiting_approval else 0, mid),
                ).rowcount or 0)
            return int(conn.execute(
                """
                UPDATE session_index
                   SET status = ?, running = 1, waiting_approval = ?
                 WHERE mission_id = ?
                """,
                (str(status or "idle"), 1 if waiting_approval else 0, mid),
            ).rowcount or 0)

        try:
            rows = self._execute_write(_do)
        except Exception as exc:
            _log.warning(
                "[doxie-session-index] update_for_mission FAILED mission_id=%s status=%s running=%s waiting=%s error=%s",
                mid, status, running, waiting_approval, exc,
            )
            return 0
        _log.debug(
            "[doxie-session-index] update_for_mission mission_id=%s status=%s running=%s waiting=%s rows=%s",
            mid, status, running, waiting_approval, rows,
        )
        return rows

    def ensure_team_mission_conversation(
        self,
        *,
        conversation_id: str = "",
        stable_session_id: str = "",
        mission: Dict[str, Any] | None = None,
        mission_id: str = "",
        team_id: str = "",
        title: str = "",
        objective: str = "",
        workspace_id: str = "",
        workspace_path: str = "",
        created_by_user_id: str = "",
        metadata: Dict[str, Any] | None = None,
        updated_at: float | None = None,
        touch: bool = False,
    ) -> Dict[str, Any]:
        mission = mission if isinstance(mission, dict) else {}
        mission_metadata = mission.get("metadata") if isinstance(mission.get("metadata"), dict) else {}
        merged_metadata = dict(mission_metadata)
        if isinstance(metadata, dict):
            merged_metadata.update(metadata)
        resolved_mission_id = _text(mission_id or mission.get("mission_id"))
        explicit_conversation_id = (
            _text(conversation_id)
            or _text(mission.get("conversation_id"))
            or _conversation_id_from_metadata(merged_metadata)
        )
        resolved_conversation_id = (
            explicit_conversation_id
            or _text((self.get_team_mission_conversation_by_session(
                _stable_session_id_from_metadata(
                    merged_metadata,
                    _text(mission.get("leader_session_id") or mission.get("team_id") or resolved_mission_id),
                )
            ) or {}).get("conversation_id"))
            or resolved_mission_id
        )
        if not resolved_conversation_id:
            return {}
        resolved_stable_session_id = (
            _text(stable_session_id)
            or _stable_session_id_from_metadata(
                merged_metadata,
                _text(mission.get("leader_session_id") or mission.get("team_id") or resolved_conversation_id),
            )
        )
        merged_metadata["conversation_id"] = resolved_conversation_id
        merged_metadata["conversation_session_id"] = resolved_stable_session_id
        merged_metadata["stableTeamSessionId"] = resolved_stable_session_id
        conversation = self.upsert_team_mission_conversation(
            conversation_id=resolved_conversation_id,
            team_id=_text(team_id or mission.get("team_id")),
            stable_session_id=resolved_stable_session_id,
            title=_text(title),
            objective=_text(objective),
            workspace_id=_text(workspace_id or mission.get("workspace_id")),
            workspace_path=_text(workspace_path or mission.get("workspace_path")),
            status="active",
            active_mission_id=resolved_mission_id,
            created_by_user_id=_text(created_by_user_id),
            updated_at=updated_at,
            metadata=merged_metadata,
            touch=touch,
        )
        if resolved_mission_id:
            def _bind(conn: sqlite3.Connection) -> None:
                conn.execute(
                    """
                    UPDATE team_missions
                    SET conversation_id = ?,
                        leader_session_id = COALESCE(NULLIF(leader_session_id, ''), ?)
                    WHERE mission_id = ?
                    """,
                    (resolved_conversation_id, resolved_stable_session_id, resolved_mission_id),
                )

            self._execute_write(_bind)
        if resolved_stable_session_id and not self.get_session(resolved_stable_session_id):
            self.create_session(resolved_stable_session_id, source="team_mission", transient=False)
        self._populate_team_conversation_participants(
            conversation_session_id=resolved_stable_session_id,
            team_id=_text(team_id or mission.get("team_id")),
            conversation_id=resolved_conversation_id,
            mission_id=resolved_mission_id,
        )
        return conversation

    def _populate_team_conversation_participants(
        self,
        *,
        conversation_session_id: str,
        team_id: str,
        conversation_id: str,
        mission_id: str,
    ) -> None:
        if not conversation_session_id:
            return
        leader_scope_subject = conversation_id or mission_id
        leader_id = leader_participant_id(leader_scope_subject)
        leader_scope_key = (
            f"team:{leader_scope_subject}:leader-conversation"
            if leader_scope_subject
            else ""
        )
        members = self.list_agent_team_members(team_id) if team_id else []
        leader_upserted = False
        for member in members:
            if not isinstance(member, dict):
                continue
            role = str(member.get("role") or "member").strip().lower()
            member_id = str(member.get("member_id") or member.get("id") or "").strip()
            agent_profile_id = str(member.get("agent_profile_id") or "").strip()
            display_name = str(member.get("name") or member.get("profile_name") or "").strip()
            avatar = str(member.get("avatar") or member.get("profile_avatar") or "").strip()
            if role == "lead":
                self.upsert_conversation_participant(
                    conversation_session_id=conversation_session_id,
                    participant_id=leader_id,
                    role="leader",
                    member_id=member_id,
                    agent_profile_id=agent_profile_id,
                    agent_profile_version_id=str(member.get("agent_profile_version_id") or ""),
                    runtime_scope_key=leader_scope_key,
                    display_name=display_name,
                    avatar=avatar,
                )
                leader_upserted = True
                continue
            if not member_id:
                continue
            member_scope = (
                str(member.get("runtime_scope_key") or "").strip()
                or f"member-chat:{conversation_id or mission_id}:{member_id}"
            )
            self.upsert_conversation_participant(
                conversation_session_id=conversation_session_id,
                participant_id=member_participant_id(member_id),
                role="member",
                member_id=member_id,
                agent_profile_id=agent_profile_id,
                agent_profile_version_id=str(member.get("agent_profile_version_id") or ""),
                runtime_scope_key=member_scope,
                display_name=display_name,
                avatar=avatar,
            )
        if not leader_upserted:
            self.upsert_conversation_participant(
                conversation_session_id=conversation_session_id,
                participant_id=leader_id,
                role="leader",
                runtime_scope_key=leader_scope_key,
            )

    def get_team_mission_conversation(self, conversation_id: str) -> Dict[str, Any]:
        conversation_id = _text(conversation_id)
        if not conversation_id:
            return {}
        with self._lock:
            return self._team_mission_conversation_from_row(self._conn.execute(
                "SELECT * FROM team_mission_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()) or {}

    def get_team_mission_conversation_by_session(self, stable_session_id: str) -> Dict[str, Any]:
        stable_session_id = _text(stable_session_id)
        if not stable_session_id:
            return {}
        with self._lock:
            return self._team_mission_conversation_from_row(self._conn.execute(
                "SELECT * FROM team_mission_conversations WHERE stable_session_id = ?",
                (stable_session_id,),
            ).fetchone()) or {}

    def resolve_team_mission_conversation(self, identifier: str) -> Dict[str, Any]:
        identifier = _text(identifier)
        if not identifier:
            return {}
        conversation = self.get_team_mission_conversation(identifier) or self.get_team_mission_conversation_by_session(identifier)
        if conversation and not _conversation_routeable(self, conversation.get("conversation_id")):
            conversation = {}
        if not conversation:
            with self._lock:
                mission = self._team_mission_from_row(self._conn.execute(
                    "SELECT * FROM team_missions WHERE mission_id = ?",
                    (identifier,),
                ).fetchone())
            if mission:
                conversation = self.ensure_team_mission_conversation(mission=mission)
        if not conversation:
            with self._lock:
                _rows = self._conn.execute(
                    "SELECT * FROM team_missions WHERE conversation_id = ? ORDER BY updated_at DESC",
                    (identifier,),
                ).fetchall()
            mission = None
            for _row in _rows:
                _m = self._team_mission_from_row(_row)
                # Skip the hidden member-chat container — never resolve to it.
                if _m and not bool((_m.get("metadata") or {}).get("member_chat_only")):
                    mission = _m
                    break
            if mission:
                conversation = self.ensure_team_mission_conversation(
                    conversation_id=identifier,
                    mission=mission,
                )
        if not conversation:
            return {}
        graph = self.get_team_mission_conversation_graph(_text(conversation.get("conversation_id")))
        messages = list((graph or {}).get("recent_messages") or [])
        page_info = (graph or {}).get("message_page_info") or {}
        return {
            "conversation": conversation,
            "mission": (graph.get("mission") if isinstance(graph, dict) else {}) or {},
            "graph": graph if isinstance(graph, dict) else {},
            "messages": messages,
            "pageInfo": page_info,
            "page_info": page_info,
        }

    def list_team_mission_conversations(
        self,
        *,
        team_id: str = "",
        workspace_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if _text(team_id):
            clauses.append("team_id = ?")
            params.append(_text(team_id))
        if _text(workspace_id):
            clauses.append("workspace_id = ?")
            params.append(_text(workspace_id))
        if _text(status):
            clauses.append("status = ?")
            params.append(_conversation_status(status))
        clauses.append(_conversation_history_sql())
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        bounded_limit = max(1, min(int(limit or 100), 500))
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT team_mission_conversations.*,
                    COALESCE(session_summary.message_count, 0) AS message_count,
                    MAX(
                        COALESCE(
                            session_summary.last_active,
                            session_summary.started_at,
                            0
                        ),
                        COALESCE(created_at, 0)
                    ) AS activity_updated_at
                FROM team_mission_conversations
                LEFT JOIN sessions session_summary
                  ON session_summary.id = team_mission_conversations.stable_session_id
                {where_sql}
                ORDER BY activity_updated_at DESC, created_at DESC, conversation_id ASC
                LIMIT ?
                """,
                (*params, bounded_limit),
            ).fetchall()
        conversations = [
            conversation for conversation in (
                self._team_mission_conversation_from_row(row)
                for row in rows
            ) if conversation is not None
        ]
        conversation_ids = [_text(item.get("conversation_id")) for item in conversations if _text(item.get("conversation_id"))]
        active_conversation_ids: set[str] = set()
        if conversation_ids:
            placeholders = ",".join("?" for _ in conversation_ids)
            with self._lock:
                active_conversation_ids = {
                    _text(_row_value(row, "conversation_id", ""))
                    for row in self._conn.execute(
                        f"""
                        SELECT DISTINCT conversation_id
                        FROM conversation_missions
                        WHERE status = 'active'
                          AND conversation_id IN ({placeholders})
                        """,
                        tuple(conversation_ids),
                    ).fetchall()
                    if _text(_row_value(row, "conversation_id", ""))
                }
        for conversation in conversations:
            conversation["running"] = _text(conversation.get("conversation_id")) in active_conversation_ids
        return conversations

    def list_team_mission_conversation_runtime_session_ids(
        self,
        *,
        team_id: str = "",
        workspace_id: str = "",
        status: str = "",
        mission_id: str = "",
        limit: int = 500,
    ) -> List[str]:
        """Return only Team Mission conversation and node runtime session ids.

        This is intentionally separate from ``list_team_mission_conversations``.
        Sidebar/runtime indexing needs identifiers, not graph, message, or
        deliverable payloads. Keeping this query narrow prevents history size
        from inflating WebSocket responses.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if _text(team_id):
            clauses.append("c.team_id = ?")
            params.append(_text(team_id))
        if _text(workspace_id):
            clauses.append("c.workspace_id = ?")
            params.append(_text(workspace_id))
        if _text(status):
            clauses.append("c.status = ?")
            params.append(_conversation_status(status))
        normalized_mission_id = _text(mission_id)
        if normalized_mission_id:
            clauses.append(
                """(
                    c.active_mission_id = ?
                    OR c.conversation_id = ?
                    OR c.stable_session_id = ?
                    OR EXISTS (
                        SELECT 1
                        FROM team_missions mission_filter
                        WHERE mission_filter.conversation_id = c.conversation_id
                          AND mission_filter.mission_id = ?
                    )
                )"""
            )
            params.extend([
                normalized_mission_id,
                normalized_mission_id,
                normalized_mission_id,
                normalized_mission_id,
            ])
        clauses.append(_conversation_history_sql("c"))
        where_sql = f"WHERE {' AND '.join(clauses)}"
        bounded_limit = max(1, min(int(limit or 500), 500))

        def append_unique(target: list[str], seen: set[str], *values: Any) -> None:
            for value in values:
                normalized = _text(value)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    target.append(normalized)

        with self._lock:
            conversation_rows = self._conn.execute(
                f"""
                SELECT c.conversation_id, c.stable_session_id
                FROM team_mission_conversations c
                {where_sql}
                ORDER BY COALESCE(c.updated_at, c.created_at, 0) DESC,
                         c.created_at DESC,
                         c.conversation_id ASC
                LIMIT ?
                """,
                (*params, bounded_limit),
            ).fetchall()

            conversation_ids = [
                _text(_row_value(row, "conversation_id", ""))
                for row in conversation_rows
                if _text(_row_value(row, "conversation_id", ""))
            ]
            stable_session_ids = [
                _text(_row_value(row, "stable_session_id", ""))
                for row in conversation_rows
                if _text(_row_value(row, "stable_session_id", ""))
            ]

            mission_rows: list[sqlite3.Row] = []
            binding_rows: list[sqlite3.Row] = []
            if conversation_ids:
                conversation_placeholders = ",".join("?" for _ in conversation_ids)
                mission_rows = self._conn.execute(
                    f"""
                    SELECT mission_id, leader_session_id
                    FROM team_missions
                    WHERE conversation_id IN ({conversation_placeholders})
                    ORDER BY created_at ASC, mission_id ASC
                    """,
                    tuple(conversation_ids),
                ).fetchall()
                mission_ids = [
                    _text(_row_value(row, "mission_id", ""))
                    for row in mission_rows
                    if _text(_row_value(row, "mission_id", ""))
                ]
                if mission_ids:
                    mission_placeholders = ",".join("?" for _ in mission_ids)
                    binding_rows = self._conn.execute(
                        f"""
                        SELECT session_id, runtime_session_id
                        FROM team_mission_run_bindings
                        WHERE mission_id IN ({mission_placeholders})
                        ORDER BY created_at ASC, run_id ASC
                        """,
                        tuple(mission_ids),
                    ).fetchall()

            active_run_rows: list[sqlite3.Row] = []
            if stable_session_ids:
                stable_placeholders = ",".join("?" for _ in stable_session_ids)
                status_placeholders = ",".join("?" for _ in _ACTIVE_RUN_STATUSES)
                active_run_rows = self._conn.execute(
                    f"""
                    SELECT session_id, runtime_session_id
                    FROM runs
                    WHERE session_id IN ({stable_placeholders})
                      AND status IN ({status_placeholders})
                    ORDER BY updated_at DESC, started_at DESC, run_id ASC
                    """,
                    (*stable_session_ids, *sorted(_ACTIVE_RUN_STATUSES)),
                ).fetchall()

        ids: list[str] = []
        seen_ids: set[str] = set()
        append_unique(ids, seen_ids, *stable_session_ids)
        for row in mission_rows:
            append_unique(ids, seen_ids, _row_value(row, "leader_session_id", ""))
        for row in active_run_rows:
            append_unique(
                ids,
                seen_ids,
                _row_value(row, "session_id", ""),
                _row_value(row, "runtime_session_id", ""),
            )
        for row in binding_rows:
            append_unique(
                ids,
                seen_ids,
                _row_value(row, "session_id", ""),
                _row_value(row, "runtime_session_id", ""),
            )
        return ids

    def _team_mission_conversation_deliverable_projection(
        self,
        conversation: Dict[str, Any],
        missions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        mission_ids = [
            _text(mission.get("mission_id"))
            for mission in missions
            if _text(mission.get("mission_id"))
        ]
        mission_id_set = set(mission_ids)
        stable_session_id = _text(
            conversation.get("stable_session_id")
            or conversation.get("stableSessionId")
        )

        last_message: Dict[str, Any] = {}
        final_deliverables: List[Dict[str, Any]] = []
        if stable_session_id:
            with self._lock:
                last_message_row = self._conn.execute(
                    """
                    SELECT *
                    FROM messages
                    WHERE session_id = ?
                      AND active = 1
                      AND role IN ('user', 'assistant')
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (stable_session_id,),
                ).fetchone()
                final_rows = self._conn.execute(
                    """
                    SELECT *
                    FROM messages
                    WHERE session_id = ?
                      AND active = 1
                      AND role = 'assistant'
                      AND metadata_json LIKE ?
                    ORDER BY id ASC
                    """,
                    (stable_session_id, "%final_deliverable%"),
                ).fetchall()
            last_message = _message_summary_from_message(self._team_mission_message_from_row(last_message_row))
            for row in final_rows:
                message = self._team_mission_message_from_row(row)
                deliverable = _final_deliverable_from_message(message)
                if not deliverable:
                    continue
                mission_id = _text(deliverable.get("mission_id"))
                if mission_id not in mission_id_set:
                    continue
                final_deliverables.append(deliverable)

        if not mission_ids:
            return {
                "last_message": last_message,
                "last_message_preview": _text(last_message.get("preview")),
                "last_message_at": last_message.get("timestamp") or 0,
                "final_deliverables": [],
                "final_deliverables_by_mission": {},
                "final_deliverables_by_task": {},
                "artifact_refs": [],
                "artifact_refs_by_mission": {},
                "artifact_refs_by_task": {},
            }

        placeholders = ",".join("?" for _ in mission_ids)
        memory_params: List[Any] = [*mission_ids, _MEMORY_COMMITTED_STATUS]
        memory_clauses = [
            f"mission_id IN ({placeholders})",
            "status = ?",
        ]
        if stable_session_id:
            memory_clauses.append("conversation_session_id = ?")
            memory_params.append(stable_session_id)
        with self._lock:
            memory_rows = self._conn.execute(
                f"""
                SELECT *
                FROM team_mission_memory_items
                WHERE {' AND '.join(memory_clauses)}
                ORDER BY created_at ASC, updated_at ASC, id ASC
                """,
                tuple(memory_params),
            ).fetchall()
        artifact_refs_by_mission: Dict[str, List[Dict[str, Any]]] = {}
        artifact_refs_by_task: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
        all_artifact_refs: List[Dict[str, Any]] = []
        for row in memory_rows:
            item = self._team_mission_memory_item_from_row(row)
            if not item:
                continue
            refs = _dedupe_artifact_refs(list(item.get("artifact_refs") or []))
            if not refs:
                continue
            mission_id = _text(item.get("mission_id"))
            task_id = _text(item.get("task_id"))
            artifact_refs_by_mission[mission_id] = _dedupe_artifact_refs([
                *artifact_refs_by_mission.get(mission_id, []),
                *refs,
            ])
            if task_id:
                artifact_refs_by_task[(mission_id, task_id)] = _dedupe_artifact_refs([
                    *artifact_refs_by_task.get((mission_id, task_id), []),
                    *refs,
                ])
            all_artifact_refs.extend(refs)

        final_deliverables = [
            _final_deliverable_with_artifact_refs(
                deliverable,
                artifact_refs_by_mission,
                artifact_refs_by_task,
            )
            for deliverable in final_deliverables
        ]
        final_deliverables_by_mission: Dict[str, List[Dict[str, Any]]] = {}
        final_deliverables_by_task: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
        for deliverable in final_deliverables:
            mission_id = _text(deliverable.get("mission_id"))
            task_id = _text(deliverable.get("task_id"))
            final_deliverables_by_mission.setdefault(mission_id, []).append(deliverable)
            if task_id:
                final_deliverables_by_task.setdefault((mission_id, task_id), []).append(deliverable)
        last_message = _message_with_deliverable_artifact_refs(last_message, final_deliverables)

        return {
            "last_message": last_message,
            "last_message_preview": _text(last_message.get("preview")),
            "last_message_at": last_message.get("timestamp") or 0,
            "final_deliverables": final_deliverables,
            "final_deliverables_by_mission": final_deliverables_by_mission,
            "final_deliverables_by_task": final_deliverables_by_task,
            "artifact_refs": _dedupe_artifact_refs(all_artifact_refs),
            "artifact_refs_by_mission": artifact_refs_by_mission,
            "artifact_refs_by_task": artifact_refs_by_task,
        }

    def get_team_mission_conversation_runtime_summary(self, conversation_id: str) -> Dict[str, Any]:
        """Return the lightweight Team Mission facts needed by conversation lists.

        The full conversation graph is still available through
        ``resolve_team_mission_conversation``. List views need a stable
        projection of task frames, active nodes, approval gates, and runtime
        session ids without loading run event history or node detail payloads.
        """
        conversation_id = _text(conversation_id)
        if not conversation_id:
            return {}
        with self._lock:
            conversation = self._team_mission_conversation_from_row(self._conn.execute(
                "SELECT * FROM team_mission_conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone())
            if conversation is None:
                return {}
            missions = [
                mission for mission in (
                    self._team_mission_from_row(row)
                    for row in self._conn.execute(
                        """
                        SELECT *
                        FROM team_missions
                        WHERE conversation_id = ?
                        ORDER BY created_at ASC, updated_at ASC, mission_id ASC
                        """,
                        (conversation_id,),
                    ).fetchall()
                ) if mission is not None
                # Hide the member-chat container: it is an internal runtime vehicle,
                # never a task the user should see on the canvas.
                and not bool((mission.get("metadata") or {}).get("member_chat_only"))
            ]
            mission_ids = [_text(mission.get("mission_id")) for mission in missions if _text(mission.get("mission_id"))]
            placeholders = ",".join("?" for _ in mission_ids)
            node_rows = self._conn.execute(
                f"""
                SELECT *
                FROM team_mission_nodes
                WHERE mission_id IN ({placeholders})
                ORDER BY created_at ASC, node_id ASC
                """,
                tuple(mission_ids),
            ).fetchall() if mission_ids else []
            binding_rows = self._conn.execute(
                f"""
                SELECT *
                FROM team_mission_run_bindings
                WHERE mission_id IN ({placeholders})
                ORDER BY created_at ASC, run_id ASC
                """,
                tuple(mission_ids),
            ).fetchall() if mission_ids else []

        if not missions:
            deliverable_projection = self._team_mission_conversation_deliverable_projection(conversation, [])
            return {
                "conversation": conversation,
                "mission": {},
                "active_mission_id": _text(conversation.get("active_mission_id")),
                "mission_status": "",
                "task_frames": [],
                "task_frame_count": 0,
                "active_task_frame": {},
                "pending_approvals": [],
                "pending_approval_count": 0,
                "active_node_count": 0,
                "run_bindings": [],
                "run_session_ids": [],
                "last_message": deliverable_projection.get("last_message") or {},
                "last_message_preview": deliverable_projection.get("last_message_preview") or "",
                "last_message_at": deliverable_projection.get("last_message_at") or 0,
                "final_deliverables": [],
                "artifact_refs": [],
            }

        nodes_by_mission: Dict[str, List[Dict[str, Any]]] = {mission_id: [] for mission_id in mission_ids}
        for row in node_rows:
            node = self._team_mission_node_from_row(row)
            if not node:
                continue
            nodes_by_mission.setdefault(_text(node.get("mission_id")), []).append(node)

        bindings: List[Dict[str, Any]] = []
        bindings_by_mission: Dict[str, List[Dict[str, Any]]] = {}
        run_session_ids: List[str] = []
        seen_run_session_ids: set[str] = set()
        for row in binding_rows:
            binding = self._team_mission_run_binding_from_row(row)
            if not binding:
                continue
            bindings.append(binding)
            bindings_by_mission.setdefault(_text(binding.get("mission_id")), []).append(binding)
            for key in ("session_id", "runtime_session_id"):
                value = _text(binding.get(key))
                if value and value not in seen_run_session_ids:
                    seen_run_session_ids.add(value)
                    run_session_ids.append(value)

        for mission_id, mission_nodes in list(nodes_by_mission.items()):
            nodes_by_mission[mission_id] = self._team_mission_nodes_with_runtime_bindings(
                mission_nodes,
                bindings_by_mission.get(mission_id) or [],
            )

        active_mission_id = _text(conversation.get("active_mission_id"))
        latest_mission = missions[-1]
        active_mission = next(
            (mission for mission in missions if _text(mission.get("mission_id")) == active_mission_id),
            latest_mission,
        )
        active_mission_id = _text(active_mission.get("mission_id")) or active_mission_id
        deliverable_projection = self._team_mission_conversation_deliverable_projection(conversation, missions)
        deliverables_by_mission = deliverable_projection.get("final_deliverables_by_mission") or {}
        deliverables_by_task = deliverable_projection.get("final_deliverables_by_task") or {}
        artifact_refs_by_mission = deliverable_projection.get("artifact_refs_by_mission") or {}
        artifact_refs_by_task = deliverable_projection.get("artifact_refs_by_task") or {}

        task_frames: List[Dict[str, Any]] = []
        pending_approvals: List[Dict[str, Any]] = []
        active_node_count = 0
        for mission in missions:
            mission_id = _text(mission.get("mission_id"))
            mission_status = _text(mission.get("status")).lower()
            mission_is_active_runtime_scope = mission_id == active_mission_id or mission_status not in _TERMINAL_MISSION_STATUSES
            mission_nodes = nodes_by_mission.get(mission_id) or []
            node_ids: List[str] = []
            root_node_id = ""
            for node in mission_nodes:
                original_node_id = _text(node.get("node_id"))
                if not original_node_id:
                    continue
                namespaced_node_id = _conversation_graph_node_id(mission_id, original_node_id)
                node_ids.append(namespaced_node_id)
                node_kind = _normalize_node_kind(_text(node.get("kind")))
                node_status = _text(node.get("status")).lower()
                if not root_node_id and node_kind == "root":
                    root_node_id = namespaced_node_id
                if mission_is_active_runtime_scope and node_status in _ACTIVE_NODE_STATUSES:
                    active_node_count += 1
                if (
                    mission_is_active_runtime_scope
                    and node_kind == "approval_gate"
                    and node_status == "waiting_approval"
                ):
                    pending_approvals.append({
                        "mission_id": mission_id,
                        "missionId": mission_id,
                        "task_frame_id": f"mission-frame:{mission_id}",
                        "taskFrameId": f"mission-frame:{mission_id}",
                        "node_id": namespaced_node_id,
                        "nodeId": namespaced_node_id,
                        "hermes_node_id": original_node_id,
                        "hermesNodeId": original_node_id,
                        "title": _text(node.get("title")) or "审批任务图",
                        "status": node_status,
                        "run_id": _text(node.get("run_id")),
                        "runId": _text(node.get("run_id")),
                        "stored_session_id": _text(node.get("stored_session_id")),
                        "storedSessionId": _text(node.get("stored_session_id")),
                        "runtime_session_id": _text(node.get("runtime_session_id")),
                        "runtimeSessionId": _text(node.get("runtime_session_id")),
                        "runtime_scope_key": _text(node.get("runtime_scope_key")),
                        "runtimeScopeKey": _text(node.get("runtime_scope_key")),
                        "runtime_binding": dict(node.get("runtime_binding") or {}),
                        "runtimeBinding": dict(node.get("runtime_binding") or {}),
                    })
            task_id = _task_id_from_mission(mission)
            frame_artifact_refs = _dedupe_artifact_refs([
                *list(artifact_refs_by_mission.get(mission_id, [])),
                *list(artifact_refs_by_task.get((mission_id, task_id), [])),
            ])
            final_deliverable = _final_deliverable_for_frame(
                deliverables_by_mission,
                deliverables_by_task,
                mission_id,
                task_id,
                frame_artifact_refs,
            )
            frame = {
                "id": f"mission-frame:{mission_id}",
                "runId": _text(mission.get("leader_session_id")),
                "missionId": mission_id,
                "mission_id": mission_id,
                "taskId": task_id,
                "task_id": task_id,
                "title": _text(mission.get("title")) or _text(conversation.get("title")) or "团队任务",
                "objective": _text(mission.get("objective")) or _text(mission.get("title")) or "团队任务",
                "status": _text(mission.get("status")) or "planning",
                "source": "hermes_conversation",
                "rootNodeId": root_node_id or (node_ids[0] if node_ids else ""),
                "root_node_id": root_node_id or (node_ids[0] if node_ids else ""),
                "nodeIds": node_ids,
                "node_ids": node_ids,
                "createdAt": mission.get("created_at") or 0,
                "created_at": mission.get("created_at") or 0,
                "updatedAt": mission.get("updated_at") or 0,
                "updated_at": mission.get("updated_at") or 0,
                "completedAt": mission.get("completed_at"),
                "completed_at": mission.get("completed_at"),
                "artifactRefs": frame_artifact_refs,
                "artifact_refs": frame_artifact_refs,
            }
            if final_deliverable:
                frame.update({
                    "finalDeliverable": final_deliverable,
                    "final_deliverable": final_deliverable,
                    "deliverableMessageId": final_deliverable.get("messageId") or "",
                    "deliverable_message_id": final_deliverable.get("message_id") or "",
                })
            task_frames.append(frame)

        active_task_frame = next(
            (frame for frame in task_frames if _text(frame.get("missionId")) == active_mission_id),
            task_frames[-1] if task_frames else {},
        )
        mission_status = _text(active_mission.get("status")) or _text(conversation.get("status"))
        # Surface in-process member-node approvals (sudo / command) and clarify
        # requests in pending_approvals too. Those live in process memory
        # (tools.approval._pending, tools.clarify_gateway._entries) — they are
        # NOT in the DB, so a sidebar that only reads team_mission_nodes never
        # learns about them. Without this, mid-mission worker tool approvals and
        # clarify requests left the sidebar showing plain "running" while the
        # composer displayed an approval card the user had to act on. Walk every
        # session key tied to this conversation (leader + every node binding's
        # session_id / runtime_session_id) and add a pending entry per pending
        # in-process request.
        runtime_session_keys: List[str] = []
        seen_runtime_session_keys: set[str] = set()
        leader_stable = _text(conversation.get("stable_session_id"))
        for candidate in (
            leader_stable,
            *(value for binding in bindings for value in (
                _text(binding.get("session_id")),
                _text(binding.get("runtime_session_id")),
            )),
        ):
            if candidate and candidate not in seen_runtime_session_keys:
                seen_runtime_session_keys.add(candidate)
                runtime_session_keys.append(candidate)
        try:
            from tools import approval as _approval_module  # noqa: WPS433
        except Exception:
            _approval_module = None
        try:
            from tools import clarify_gateway as _clarify_module  # noqa: WPS433
        except Exception:
            _clarify_module = None
        for session_key in runtime_session_keys:
            if (
                _approval_module is not None
                and getattr(_approval_module, "has_pending_session", None)
            ):
                try:
                    if _approval_module.has_pending_session(session_key):
                        pending_approvals.append({
                            "kind": "tool_approval",
                            "mission_id": active_mission_id,
                            "missionId": active_mission_id,
                            "session_key": session_key,
                            "sessionKey": session_key,
                            "title": "等待工具审批",
                            "source": "tools.approval._pending",
                        })
                except Exception:
                    pass
            if (
                _clarify_module is not None
                and getattr(_clarify_module, "has_pending", None)
            ):
                try:
                    if _clarify_module.has_pending(session_key):
                        pending_approvals.append({
                            "kind": "clarify",
                            "mission_id": active_mission_id,
                            "missionId": active_mission_id,
                            "session_key": session_key,
                            "sessionKey": session_key,
                            "title": "等待澄清回答",
                            "source": "tools.clarify_gateway._entries",
                        })
                except Exception:
                    pass
        return {
            "conversation": conversation,
            "mission": active_mission,
            "active_mission_id": active_mission_id,
            "mission_status": mission_status,
            "task_frames": task_frames,
            "task_frame_count": len(task_frames),
            "active_task_frame": active_task_frame,
            "pending_approvals": pending_approvals,
            "pending_approval_count": len(pending_approvals),
            "active_node_count": active_node_count,
            "run_bindings": bindings,
            "run_session_ids": run_session_ids,
            "last_message": deliverable_projection.get("last_message") or {},
            "last_message_preview": deliverable_projection.get("last_message_preview") or "",
            "last_message_at": deliverable_projection.get("last_message_at") or 0,
            "final_deliverables": list(deliverable_projection.get("final_deliverables") or []),
            "artifact_refs": list(deliverable_projection.get("artifact_refs") or []),
        }

    def _team_mission_conversation_active_run(self, stable_session_id: str) -> Dict[str, Any]:
        stable_session_id = _text(stable_session_id)
        if not stable_session_id:
            return {}
        placeholders = ",".join("?" for _ in _ACTIVE_RUN_STATUSES)
        with self._lock:
            row = self._conn.execute(
                f"""
                SELECT *
                FROM runs
                WHERE session_id = ?
                  AND status IN ({placeholders})
                ORDER BY updated_at DESC, started_at DESC, run_id ASC
                LIMIT 1
                """,
                (stable_session_id, *sorted(_ACTIVE_RUN_STATUSES)),
            ).fetchone()
        try:
            return self._run_from_row(row) or {}
        except Exception:
            return {}

    def _team_mission_active_node_run_from_bindings(
        self,
        bindings: List[Dict[str, Any]] | None,
    ) -> Dict[str, Any]:
        active_run: Dict[str, Any] = {}
        for binding in bindings or []:
            if not isinstance(binding, dict):
                continue
            run_id = _text(binding.get("run_id") or binding.get("runId"))
            if not run_id:
                continue
            run = self.get_run(run_id) if hasattr(self, "get_run") else None
            if _text((run or {}).get("status")).lower() not in _ACTIVE_RUN_STATUSES:
                continue
            merged_run = {
                **binding,
                **dict(run or {}),
                "run_id": run_id,
                "runtime_session_id": _text((run or {}).get("runtime_session_id") or binding.get("runtime_session_id")),
                "runtime_scope_key": _text((run or {}).get("runtime_scope_key") or binding.get("runtime_scope_key")),
                "turn_id": _text((run or {}).get("turn_id") or binding.get("turn_id")),
            }
            if not active_run or float(merged_run.get("updated_at") or 0) >= float(active_run.get("updated_at") or 0):
                active_run = merged_run
        return active_run

    def _team_mission_conversation_message_count(self, stable_session_id: str) -> int:
        stable_session_id = _text(stable_session_id)
        if not stable_session_id:
            return 0
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(message_count, 0) AS message_count FROM sessions WHERE id = ?",
                (stable_session_id,),
            ).fetchone()
        try:
            return int(_row_value(row, "message_count", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def get_team_mission_conversation_status_projection(self, conversation_id: str) -> Dict[str, Any]:
        """Return the canonical Team Mission conversation status event payload.

        This is the event-stream counterpart to ``team_mission.conversation.list``:
        it projects only sidebar/index facts and keeps raw runtime trace in
        ``run_events``.
        """
        conversation_id = _text(conversation_id)
        if not conversation_id:
            return {}
        summary = self.get_team_mission_conversation_runtime_summary(conversation_id)
        if not isinstance(summary, dict) or not summary:
            return {}
        conversation = summary.get("conversation") if isinstance(summary.get("conversation"), dict) else {}
        conversation = dict(conversation or self.get_team_mission_conversation(conversation_id) or {})
        if not conversation:
            return {}
        stable_session_id = _text(conversation.get("stable_session_id") or conversation.get("stableSessionId"))
        active_run = self._team_mission_conversation_active_run(stable_session_id)
        active_node_run = self._team_mission_active_node_run_from_bindings(
            [
                item for item in summary.get("run_bindings") or []
                if isinstance(item, dict)
            ],
        )
        pending_approvals = [
            item for item in summary.get("pending_approvals") or []
            if isinstance(item, dict)
        ]
        mission_status = _text(summary.get("mission_status"))
        has_active_mission = self.has_active_mission(conversation_id)
        # Resolve the run state from the ACTIVE MISSION's real status. Never fall
        # back to conversation.get("status") — that is the conversation lifecycle
        # state ('active' = not archived), NOT a run state, and treating it as
        # non-terminal made finished team conversations show as "running".
        if not mission_status:
            active_mission_id = _text(
                conversation.get("active_mission_id") or conversation.get("activeMissionId")
            )
            if active_mission_id:
                with self._lock:
                    mission_row = self._conn.execute(
                        "SELECT status FROM team_missions WHERE mission_id = ?",
                        (active_mission_id,),
                    ).fetchone()
                if mission_row is not None:
                    mission_status = _text(_row_value(mission_row, "status", ""))
        active_node_count = int(summary.get("active_node_count") or 0)
        if active_node_run and active_node_count <= 0:
            active_node_count = 1
        # The sidebar "running" flag means runtime work is currently observed,
        # not merely that a conversation still has a linked active mission row.
        # - active_run / active_node_run: keeps subscribe-path projections running
        #   while the run reducer has not yet marked the run terminal.
        # - mission_terminal: terminal mission state wins over zombie running rows.
        # - has_active_mission: scopes the observed run to an active conversation
        #   mission link instead of any historical run binding.
        mission_terminal = mission_status in _TERMINAL_MISSION_STATUSES
        run_observed = bool(active_run) or bool(active_node_run)
        running = run_observed and not mission_terminal and has_active_mission
        waiting_approval = bool(pending_approvals) or mission_status == "waiting_approval"
        projected_state = "waiting_approval" if waiting_approval else "running" if running else (
            "completed" if mission_status == "completed"
            else "failed" if mission_status == "failed"
            else "cancelled" if mission_status in {"cancelled", "canceled", "interrupted"}
            else "idle"
        )
        projected_active_run = active_run or active_node_run
        active_mission = summary.get("mission") if isinstance(summary.get("mission"), dict) else {}
        mission_started_at = float((active_mission or {}).get("created_at") or 0)
        mission_updated_at = float((active_mission or {}).get("updated_at") or 0)
        mission_completed_at = float((active_mission or {}).get("completed_at") or 0)
        run_updated_at = max(
            float((active_run or {}).get("updated_at") or 0),
            float((active_node_run or {}).get("updated_at") or 0),
        )
        last_message_at = summary.get("last_message_at") or 0
        updated_at = max(
            float(conversation.get("updated_at") or 0),
            float(last_message_at or 0),
            float(run_updated_at or 0),
        )
        projection = {
            **conversation,
            "conversation_id": conversation_id,
            "stable_session_id": stable_session_id,
            "team_id": _text(conversation.get("team_id")),
            "active_mission_id": _text(summary.get("active_mission_id") or conversation.get("active_mission_id")),
            "activeMissionId": _text(summary.get("active_mission_id") or conversation.get("active_mission_id")),
            "mission_status": mission_status,
            "status": mission_status or _text(conversation.get("status")),
            "running": running,
            "run_state": projected_state,
            "activity_state": projected_state,
            "waiting_approval": waiting_approval,
            "pending_approval_count": len(pending_approvals),
            "pending_approvals": pending_approvals,
            "active_run_id": _text(projected_active_run.get("run_id")) if running and projected_active_run else "",
            "active_turn_id": _text(projected_active_run.get("turn_id")) if running and projected_active_run else "",
            "active_runtime_session_id": _text(projected_active_run.get("runtime_session_id")) if running and projected_active_run else "",
            "runtime_scope_key": _text(projected_active_run.get("runtime_scope_key")) if running and projected_active_run else "",
            "run_started_at": projected_active_run.get("started_at") or 0 if running and projected_active_run else 0,
            "run_updated_at": run_updated_at,
            "mission_started_at": mission_started_at,
            "mission_updated_at": mission_updated_at,
            "mission_completed_at": mission_completed_at,
            "active_node_count": active_node_count,
            "task_frames": list(summary.get("task_frames") or []),
            "task_frame_count": int(summary.get("task_frame_count") or 0),
            "active_task_frame": summary.get("active_task_frame") if isinstance(summary.get("active_task_frame"), dict) else {},
            "run_session_ids": list(summary.get("run_session_ids") or []),
            "last_message": summary.get("last_message") if isinstance(summary.get("last_message"), dict) else {},
            "last_message_preview": _text(summary.get("last_message_preview")),
            "last_message_at": last_message_at,
            "final_deliverables": list(summary.get("final_deliverables") or []),
            "artifact_refs": list(summary.get("artifact_refs") or []),
            "message_count": int(conversation.get("message_count") or 0) or self._team_mission_conversation_message_count(stable_session_id),
            "updated_at": updated_at,
        }
        return projection

    def _append_team_mission_state_projection_event(
        self,
        *,
        mission_id: str,
        event: Dict[str, Any],
        identity: Dict[str, str] | None = None,
        dedupe_key: str = "",
    ) -> Dict[str, Any]:
        stored = self.append_team_mission_structural_event(
            mission_id=mission_id,
            source_event=event,
            identity=identity,
            dedupe_key=dedupe_key,
        )
        source_seq = _event_seq(stored)
        if source_seq > 0:
            try:
                self.append_team_mission_conversation_status_event(
                    mission_id=mission_id,
                    source_event=event,
                    source_mission_seq=source_seq,
                )
            except Exception:
                pass
        return stored

    def _team_mission_conversation_status_event(
        self,
        *,
        mission_id: str,
        source_event: Dict[str, Any],
        source_seq: int,
        projection_seq: int,
    ) -> Dict[str, Any]:
        mission_id = _text(mission_id)
        if not mission_id:
            return {}
        with self._lock:
            mission = self._team_mission_from_row(self._conn.execute(
                "SELECT * FROM team_missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone())
        conversation_id = _text((mission or {}).get("conversation_id"))
        if not conversation_id:
            return {}
        projection = self.get_team_mission_conversation_status_projection(conversation_id)
        if not projection:
            return {}
        stable_session_id = _text(projection.get("stable_session_id"))
        source_type = _text(source_event.get("type"))
        source_run_id = _text(source_event.get("run_id") or source_event.get("runId"))
        timestamp = float(source_event.get("timestamp") or time.time())
        payload = {
            "conversation_id": conversation_id,
            "conversationId": conversation_id,
            "stable_session_id": stable_session_id,
            "stableSessionId": stable_session_id,
            "mission_id": mission_id,
            "missionId": mission_id,
            "active_mission_id": _text(projection.get("active_mission_id")) or mission_id,
            "activeMissionId": _text(projection.get("active_mission_id")) or mission_id,
            "source_event_type": source_type,
            "sourceEventType": source_type,
            "source_run_id": source_run_id,
            "sourceRunId": source_run_id,
            "source_seq": source_seq,
            "sourceSeq": source_seq,
            "team_mission_event_seq": projection_seq,
            "teamMissionEventSeq": projection_seq,
            "projection": projection,
            "conversation": projection,
        }
        return {
            "type": _TEAM_MISSION_CONVERSATION_STATUS_EVENT_TYPE,
            "seq": projection_seq,
            "source_seq": source_seq,
            "team_mission_event_seq": projection_seq,
            "timestamp": timestamp,
            "mission_id": mission_id,
            "missionId": mission_id,
            "source_run_id": source_run_id,
            "sourceRunId": source_run_id,
            "conversation_id": conversation_id,
            "conversationId": conversation_id,
            "stable_session_id": stable_session_id,
            "stableSessionId": stable_session_id,
            "payload": payload,
        }

    def rename_team_mission_conversation(self, identifier: str, title: str) -> Dict[str, Any]:
        return _rename_team_mission_conversation(self, identifier, title)

    def delete_team_mission_conversation(self, identifier: str) -> Dict[str, Any]:
        return _delete_team_mission_conversation(self, identifier)
