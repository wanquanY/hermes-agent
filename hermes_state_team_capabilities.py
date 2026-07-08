from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict

from hermes_team_capability_snapshot import build_snapshot_view
from hermes_team_capability_snapshot import normalize_mapping
from hermes_team_capability_snapshot import normalize_source_packet
from hermes_team_capability_snapshot import snapshot_from_storage
from hermes_team_capability_snapshot import snapshot_storage_fields
from hermes_team_capability_snapshot import source_digest
from hermes_team_capability_snapshot import text as _text


def _row_value(row: sqlite3.Row | None, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


class TeamCapabilityStateMixin:
    """Canonical Hermes team capability snapshot persistence.

    DoXie supplies source facts. Hermes owns capability snapshot generation,
    versioning, staleness, and Team Mission bindings.
    """

    def _team_capability_snapshot_from_row(self, row: sqlite3.Row | None) -> Dict[str, Any]:
        if row is None:
            return {}
        return snapshot_from_storage({
            "snapshot_id": _row_value(row, "snapshot_id", ""),
            "team_id": _row_value(row, "team_id", ""),
            "version": _row_value(row, "version", 0),
            "status": _row_value(row, "status", ""),
            "source_digest": _row_value(row, "source_digest", ""),
            "source_packet_digest": _row_value(row, "source_packet_digest", ""),
            "team_profile_json": _row_value(row, "team_profile_json", ""),
            "member_profiles_json": _row_value(row, "member_profiles_json", ""),
            "capability_axes_json": _row_value(row, "capability_axes_json", ""),
            "assignment_policy_json": _row_value(row, "assignment_policy_json", ""),
            "evidence_refs_json": _row_value(row, "evidence_refs_json", ""),
            "stale_reason": _row_value(row, "stale_reason", ""),
            "generated_at": _row_value(row, "generated_at", 0),
            "updated_at": _row_value(row, "updated_at", 0),
        })

    def _team_capability_binding_from_row(self, row: sqlite3.Row | None) -> Dict[str, Any]:
        if row is None:
            return {}
        return {
            "binding_id": _text(_row_value(row, "binding_id", "")),
            "mission_id": _text(_row_value(row, "mission_id", "")),
            "conversation_id": _text(_row_value(row, "conversation_id", "")),
            "snapshot_id": _text(_row_value(row, "snapshot_id", "")),
            "snapshot_version": int(_row_value(row, "snapshot_version", 0) or 0),
            "source_digest": _text(_row_value(row, "source_digest", "")),
            "pinned_at": float(_row_value(row, "pinned_at", 0) or 0),
        }

    def get_team_capability_snapshot(self, snapshot_id: str) -> Dict[str, Any]:
        snapshot_id = _text(snapshot_id)
        if not snapshot_id:
            return {}
        with self._lock:
            return self._team_capability_snapshot_from_row(self._conn.execute(
                "SELECT * FROM team_capability_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone())

    def get_latest_team_capability_snapshot(self, team_id: str) -> Dict[str, Any]:
        team_id = _text(team_id)
        if not team_id:
            return {}
        with self._lock:
            return self._team_capability_snapshot_from_row(self._conn.execute(
                """
                SELECT * FROM team_capability_snapshots
                WHERE team_id = ?
                ORDER BY version DESC, updated_at DESC
                LIMIT 1
                """,
                (team_id,),
            ).fetchone())

    def resolve_team_capability_snapshot(
        self,
        *,
        team_id: str = "",
        source_packet: Dict[str, Any] | None = None,
        source_digest_value: str = "",
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        packet = normalize_source_packet(source_packet or {})
        resolved_team_id = _text(team_id or packet.get("teamId"))
        if not resolved_team_id:
            raise ValueError("team_id required")
        digest = _text(source_digest_value) or source_digest(packet)
        latest = self.get_latest_team_capability_snapshot(resolved_team_id)
        if latest and not force_refresh:
            if _text(latest.get("source_digest")) == digest and _text(latest.get("status")) == "ready":
                return latest
            return self.mark_team_capability_snapshot_stale(
                snapshot_id=_text(latest.get("snapshot_id")),
                reason="source packet changed",
            )
        version = int(latest.get("version") or 0) + 1 if latest else 1
        snapshot_id = f"team-capability:{resolved_team_id}:v{version}"
        now = time.time()
        snapshot = build_snapshot_view(
            snapshot_id=snapshot_id,
            team_id=resolved_team_id,
            version=version,
            source_packet=packet,
            digest=digest,
            status="ready",
            generated_at=now,
            updated_at=now,
        )
        return self.upsert_team_capability_snapshot(snapshot)

    def upsert_team_capability_snapshot(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        normalized = normalize_mapping(snapshot)
        snapshot_id = _text(normalized.get("snapshot_id"))
        team_id = _text(normalized.get("team_id"))
        if not snapshot_id:
            raise ValueError("snapshot_id required")
        if not team_id:
            raise ValueError("team_id required")
        version = int(normalized.get("version") or 1)
        now = time.time()
        generated_at = float(normalized.get("generated_at") or now)
        updated_at = float(normalized.get("updated_at") or now)
        storage = snapshot_storage_fields(normalized)

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            conn.execute(
                """
                INSERT INTO team_capability_snapshots (
                    snapshot_id, team_id, version, status, source_digest, source_packet_digest,
                    team_profile_json, member_profiles_json, capability_axes_json,
                    assignment_policy_json, evidence_refs_json, stale_reason,
                    generated_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_id) DO UPDATE SET
                    status = excluded.status,
                    source_digest = excluded.source_digest,
                    source_packet_digest = excluded.source_packet_digest,
                    team_profile_json = excluded.team_profile_json,
                    member_profiles_json = excluded.member_profiles_json,
                    capability_axes_json = excluded.capability_axes_json,
                    assignment_policy_json = excluded.assignment_policy_json,
                    evidence_refs_json = excluded.evidence_refs_json,
                    stale_reason = excluded.stale_reason,
                    updated_at = excluded.updated_at
                """,
                (
                    snapshot_id,
                    team_id,
                    version,
                    _text(normalized.get("status") or "ready"),
                    _text(normalized.get("source_digest")),
                    _text(normalized.get("source_packet_digest") or normalized.get("source_digest")),
                    storage["team_profile_json"],
                    storage["member_profiles_json"],
                    storage["capability_axes_json"],
                    storage["assignment_policy_json"],
                    storage["evidence_refs_json"],
                    _text(normalized.get("stale_reason")),
                    generated_at,
                    updated_at,
                ),
            )
            return self._team_capability_snapshot_from_row(conn.execute(
                "SELECT * FROM team_capability_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone())

        return self._execute_write(_do)

    def mark_team_capability_snapshot_stale(self, *, snapshot_id: str, reason: str = "") -> Dict[str, Any]:
        snapshot_id = _text(snapshot_id)
        if not snapshot_id:
            return {}

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            conn.execute(
                """
                UPDATE team_capability_snapshots
                SET status = 'stale',
                    stale_reason = ?,
                    updated_at = ?
                WHERE snapshot_id = ?
                """,
                (_text(reason) or "source packet changed", time.time(), snapshot_id),
            )
            return self._team_capability_snapshot_from_row(conn.execute(
                "SELECT * FROM team_capability_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone())

        return self._execute_write(_do)

    def bind_team_capability_snapshot(
        self,
        *,
        mission_id: str,
        conversation_id: str = "",
        snapshot_id: str,
    ) -> Dict[str, Any]:
        mission_id = _text(mission_id)
        snapshot_id = _text(snapshot_id)
        if not mission_id:
            raise ValueError("mission_id required")
        snapshot = self.get_team_capability_snapshot(snapshot_id)
        if not snapshot:
            raise ValueError("team capability snapshot not found")
        resolved_conversation_id = _text(conversation_id)
        if not resolved_conversation_id:
            try:
                mission = self.get_team_mission_graph(mission_id).get("mission") or {}
                resolved_conversation_id = _text(mission.get("conversation_id"))
            except Exception:
                resolved_conversation_id = ""
        binding_id = f"team-capability-binding:{mission_id}"
        pinned_at = time.time()

        def _do(conn: sqlite3.Connection) -> Dict[str, Any]:
            conn.execute(
                """
                INSERT INTO team_capability_snapshot_bindings (
                    binding_id, mission_id, conversation_id, snapshot_id,
                    snapshot_version, source_digest, pinned_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mission_id) DO UPDATE SET
                    conversation_id = excluded.conversation_id,
                    snapshot_id = excluded.snapshot_id,
                    snapshot_version = excluded.snapshot_version,
                    source_digest = excluded.source_digest,
                    pinned_at = excluded.pinned_at
                """,
                (
                    binding_id,
                    mission_id,
                    resolved_conversation_id,
                    snapshot_id,
                    int(snapshot.get("version") or 0),
                    _text(snapshot.get("source_digest")),
                    pinned_at,
                ),
            )
            return self._team_capability_binding_from_row(conn.execute(
                "SELECT * FROM team_capability_snapshot_bindings WHERE mission_id = ?",
                (mission_id,),
            ).fetchone())

        binding = self._execute_write(_do)
        self._merge_team_mission_capability_metadata(mission_id=mission_id, binding=binding)
        return binding

    def get_team_capability_snapshot_binding(self, mission_id: str) -> Dict[str, Any]:
        mission_id = _text(mission_id)
        if not mission_id:
            return {}
        with self._lock:
            return self._team_capability_binding_from_row(self._conn.execute(
                "SELECT * FROM team_capability_snapshot_bindings WHERE mission_id = ?",
                (mission_id,),
            ).fetchone())

    def get_bound_team_capability_snapshot(self, mission_id: str) -> Dict[str, Any]:
        binding = self.get_team_capability_snapshot_binding(mission_id)
        snapshot_id = _text(binding.get("snapshot_id"))
        return self.get_team_capability_snapshot(snapshot_id) if snapshot_id else {}

    def _merge_team_mission_capability_metadata(self, *, mission_id: str, binding: Dict[str, Any]) -> None:
        mission_id = _text(mission_id)
        if not mission_id or not binding:
            return
        try:
            graph = self.get_team_mission_graph(mission_id)
            mission = graph.get("mission") if isinstance(graph, dict) else {}
            if not mission:
                return
            metadata = dict(mission.get("metadata") or {})
            metadata["team_capability_snapshot"] = {
                "snapshot_id": _text(binding.get("snapshot_id")),
                "snapshot_version": int(binding.get("snapshot_version") or 0),
                "source_digest": _text(binding.get("source_digest")),
                "pinned_at": float(binding.get("pinned_at") or 0),
            }
            self.upsert_team_mission(
                mission_id=mission_id,
                conversation_id=_text(mission.get("conversation_id")),
                team_id=_text(mission.get("team_id")),
                title=_text(mission.get("title")),
                objective=_text(mission.get("objective")),
                workspace_id=_text(mission.get("workspace_id")),
                workspace_path=_text(mission.get("workspace_path")),
                mode=_text(mission.get("mode")),
                status=_text(mission.get("status")),
                leader_session_id=_text(mission.get("leader_session_id")),
                metadata=metadata,
            )
        except Exception:
            return
