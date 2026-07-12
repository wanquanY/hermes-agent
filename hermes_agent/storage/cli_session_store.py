"""SQLite session store for the interactive CLI.

This is the CLI-facing persistence owner used during the P2 state-facade
retirement. It deliberately talks to SQLite/repositories directly and does not
import the legacy state facade.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from channels.platforms.telegram_topic_store import TelegramTopicStore
from hermes_agent.domain.activity_service import ActivityService
from hermes_agent.domain.compression_lease_service import CompressionLeaseService
from hermes_agent.domain.message_service import MessageService
from hermes_agent.domain.member_chat_projection_service import MemberChatProjectionService
from hermes_agent.domain.participant_service import ParticipantService
from hermes_agent.domain.run_event_maintenance_service import RunEventMaintenanceService
from hermes_agent.domain.run_service import RunService
from hermes_agent.domain.session_analytics_service import SessionAnalyticsService
from hermes_agent.domain.session_branch_service import SessionBranchService
from hermes_agent.domain.session_deletion import SessionDeletionService
from hermes_agent.domain.session_index_service import SessionIndexService
from hermes_agent.domain.session_service import SessionService
from hermes_agent.domain.state_metadata_service import StateMetadataService
from hermes_agent.domain.storage_maintenance_service import StorageMaintenanceService
from hermes_agent.domain.team_capability_service import TeamCapabilityService
from hermes_agent.domain.team_mission_audit_log import TeamMissionAuditLog
from hermes_agent.domain.team_mission_audit_service import TeamMissionAuditService
from hermes_agent.domain.team_mission_maintenance_service import (
    TeamMissionMaintenanceService,
)
from hermes_agent.read_models.session_recall import SessionRecallReadModel
from hermes_agent.read_models.team_missions import TeamMissionReadModel
from hermes_agent.read_models.tool_events import ToolEventProjectionReadModel
from hermes_agent.repositories.agent_profile_repo import AgentProfileRepoImpl
from hermes_agent.repositories.compression_lease_repo import CompressionLeaseRepository
from hermes_agent.repositories.conversation_participant_repo import ConversationParticipantRepo
from hermes_agent.repositories.session_repo import SessionRepoImpl
from hermes_agent.repositories.team_capability_repo import TeamCapabilityRepo
from hermes_agent.repositories.team_mission_repo import TeamMissionRepoImpl
from hermes_agent.repositories.team_registry_repo import TeamRegistryRepo
from hermes_agent.storage.session_repository_db import connect_session_repository_db
from hermes_agent.storage.sqlite_connection_lock import lock_for_connection
from hermes_agent.storage.unit_of_work import SqliteUnitOfWork
from hermes_team_mission.domain.transcript_visibility import (
    TeamMissionTranscriptVisibilityPolicy,
)
from hermes_team_mission.read_models.conversation_deliverables import (
    ConversationDeliverableReadModel,
)
from hermes_team_mission.read_models.graph_query import TeamMissionGraphQueryService
from hermes_team_mission.read_models.node_history import (
    TeamMissionNodeHistoryReadModel,
)
from hermes_team_mission.read_models.row_mapper import TeamMissionRowMapper
from hermes_team_mission.runtime.team_transcript_writer import RuntimeTranscriptWriter
from hermes_team_mission.runtime.transcript_projection_service import (
    TeamTranscriptProjectionService,
)
from hermes_team_mission.state.maintenance import run_team_mission_startup_maintenance
from hermes_team_mission.state.session_mixin import TeamMissionStateMixin


logger = logging.getLogger(__name__)


def open_cli_session_store(db_path: Path | str | None = None):
    return CliSessionStore(connect_session_repository_db(db_path))


class CliSessionStore(TeamMissionStateMixin):
    """Method surface currently required by CLI and AIAgent persistence."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = lock_for_connection(conn)
        self._unit_of_work = SqliteUnitOfWork(conn, self._lock)
        self._session_repo = SessionRepoImpl(conn)
        self.team_mission_rows = TeamMissionRowMapper()
        self.team_mission_conversation_deliverables = ConversationDeliverableReadModel(
            conn,
            self.team_mission_rows,
        )
        self.telegram_topics = TelegramTopicStore(
            conn,
            self._execute_write,
            self._lock,
        )
        self.compression_leases = CompressionLeaseService(
            CompressionLeaseRepository(conn),
            self._unit_of_work,
        )
        self.profiles = AgentProfileRepoImpl(conn)
        self.teams = TeamRegistryRepo(conn, self._execute_write, self._lock)
        participant_repository = ConversationParticipantRepo(
            conn,
            self._execute_write,
            self._lock,
        )
        self.participants = ParticipantService(
            participant_repository,
            self._unit_of_work,
        )
        self.participants.reconcile()
        activity_repository = TeamMissionRepoImpl(conn)
        self.activities = ActivityService(activity_repository, self._unit_of_work)
        self.team_mission_maintenance = TeamMissionMaintenanceService(
            activity_repository,
            self._unit_of_work,
        )
        self.team_mission_audit = TeamMissionAuditService(
            TeamMissionAuditLog(conn),
            self._unit_of_work,
            self._lock,
        )
        self.team_missions = TeamMissionReadModel(conn)
        self.team_mission_node_history = TeamMissionNodeHistoryReadModel(conn)
        self._session_deletion = SessionDeletionService(
            conn,
            session_repo=self._session_repo,
            unit_of_work=self._unit_of_work,
        )
        self.metadata = StateMetadataService(conn, self._unit_of_work)
        self.analytics = SessionAnalyticsService(conn)
        self.maintenance = StorageMaintenanceService(
            conn,
            self._lock,
            self._unit_of_work,
            self._session_repo,
            self._session_deletion,
            self.metadata,
        )
        self.messages = MessageService(
            conn,
            self._session_repo,
            unit_of_work=self._unit_of_work,
            visibility_policies={"team": TeamMissionTranscriptVisibilityPolicy()},
        )
        self.team_transcript_projections = TeamTranscriptProjectionService(
            self,
            self._unit_of_work,
        )
        self.team_mission_graphs = TeamMissionGraphQueryService(
            conn,
            self.team_mission_rows,
            self.team_mission_conversation_deliverables,
            self.messages,
        )
        self._recall = SessionRecallReadModel(conn)
        self.sessions = SessionService(
            conn,
            self._session_repo,
            self._recall,
            self.messages,
            self._unit_of_work,
            deletion=self._session_deletion,
        )
        self.session_index = SessionIndexService(
            conn,
            self._session_repo,
            self._unit_of_work,
            self._repair_session_index_active_team_runtime_scope_locked,
        )
        self.branches = SessionBranchService(
            conn,
            self._execute_write,
            self._lock,
            self.sessions.sanitize_title,
        )
        self.member_chat_views = MemberChatProjectionService(
            conn,
            self._execute_write,
            self.messages.list,
            self.messages.append,
        )
        self.runs = RunService(
            conn,
            self._unit_of_work,
            self._session_repo,
            event_normalizer=lambda session_id, event: (
                RuntimeTranscriptWriter.normalize_message_complete_event(
                    self,
                    session_id=session_id,
                    event=event,
                )
            ),
            message_complete_projector=lambda session_id, event: (
                RuntimeTranscriptWriter.project_message_complete_event_locked(
                    self,
                    conn,
                    session_id=session_id,
                    event=event,
                )
            ),
        )
        self.run_event_maintenance = RunEventMaintenanceService(
            conn,
            self._unit_of_work,
            self.metadata,
        )
        self.tool_event_projection = ToolEventProjectionReadModel(conn)
        self.team_capabilities = TeamCapabilityService(
            TeamCapabilityRepo(conn, self._execute_write, self._lock),
            self.team_mission_graphs,
            self.upsert_team_mission,
        )
        run_team_mission_startup_maintenance(self, logger)
        self.session_index.reconcile()

    @property
    def db_path(self) -> Path:
        row = self._conn.execute("PRAGMA database_list").fetchone()
        if row is None:
            return Path("")
        file_value = row["file"] if "file" in row.keys() else row[2]
        return Path(str(file_value or ""))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _execute_write(self, fn):
        return self._unit_of_work.execute(fn)


__all__ = ["CliSessionStore", "open_cli_session_store"]
