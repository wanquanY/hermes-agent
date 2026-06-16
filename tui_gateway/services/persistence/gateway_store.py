"""SQLite persistence for gateway-owned workspace and artifact state."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

DEFAULT_ARTIFACT_RETENTION_DAYS = 30
DEFAULT_ARTIFACT_MAX_PER_SESSION = 500
DEFAULT_ARTIFACT_MAX_PER_WORKSPACE = 2000

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS gateway_workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS gateway_session_workspaces (
    session_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES gateway_workspaces(id)
);

CREATE TABLE IF NOT EXISTS gateway_artifacts (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    path TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    title TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    origin_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (workspace_id, path),
    FOREIGN KEY (workspace_id) REFERENCES gateway_workspaces(id)
);

CREATE TABLE IF NOT EXISTS gateway_session_artifacts (
    session_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    PRIMARY KEY (session_id, artifact_id),
    FOREIGN KEY (artifact_id) REFERENCES gateway_artifacts(id)
);

CREATE TABLE IF NOT EXISTS gateway_session_toolsets (
    session_id TEXT PRIMARY KEY,
    enabled_toolsets_json TEXT,
    disabled_toolsets_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gateway_artifacts_workspace
    ON gateway_artifacts(workspace_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_gateway_session_artifacts_session
    ON gateway_session_artifacts(session_id, last_seen_at DESC);
"""


class GatewayStateStore:
    """Small gateway-owned store separate from Hermes transcript state."""

    def __init__(self, db_path: Path | None = None, *, create_if_missing: bool = True):
        self.db_path = db_path or get_hermes_home() / "tui-gateway" / "state.db"
        self._lock = threading.Lock()
        self._create_if_missing = bool(create_if_missing)
        if self._create_if_missing or self.db_path.exists():
            self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        if not self._create_if_missing and not self.db_path.exists():
            raise FileNotFoundError(str(self.db_path))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=1.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        if self._create_if_missing:
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(gateway_session_workspaces)").fetchall()
            }
            if "metadata_json" not in columns:
                conn.execute(
                    "ALTER TABLE gateway_session_workspaces ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'"
                )

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
        return dict(row) if row else None

    def upsert_workspace(self, workspace: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO gateway_workspaces
                        (id, name, path, kind, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        path = excluded.path,
                        kind = excluded.kind,
                        updated_at = excluded.updated_at
                    """,
                    (
                        workspace["id"],
                        workspace["name"],
                        workspace["path"],
                        workspace.get("kind", ""),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                conn.execute(
                    """
                    UPDATE gateway_workspaces
                    SET name = ?, kind = ?, updated_at = ?
                    WHERE path = ?
                    """,
                    (
                        workspace["name"],
                        workspace.get("kind", ""),
                        now,
                        workspace["path"],
                    ),
                )
            row = conn.execute(
                "SELECT * FROM gateway_workspaces WHERE path = ?",
                (workspace["path"],),
            ).fetchone()
        return self._row_to_dict(row) or dict(workspace)

    def bind_session_workspace(
        self,
        *,
        session_id: str,
        workspace_id: str,
        cwd: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO gateway_session_workspaces
                    (session_id, workspace_id, cwd, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    workspace_id = excluded.workspace_id,
                    cwd = excluded.cwd,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (session_id, workspace_id, cwd, metadata_json, now, now),
            )
            row = conn.execute(
                """
                SELECT sw.session_id, sw.cwd, sw.metadata_json,
                       sw.created_at AS binding_created_at,
                       sw.updated_at AS binding_updated_at,
                       sw.workspace_id AS id, w.name, w.path, w.kind,
                       w.created_at, w.updated_at
                FROM gateway_session_workspaces sw
                JOIN gateway_workspaces w ON w.id = sw.workspace_id
                WHERE sw.session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return self._session_workspace_row_to_payload(row)

    def get_session_workspace(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT sw.session_id, sw.cwd, sw.metadata_json,
                       sw.created_at AS binding_created_at,
                       sw.updated_at AS binding_updated_at,
                       sw.workspace_id AS id,
                       w.name, w.path, w.kind, w.created_at, w.updated_at
                FROM gateway_session_workspaces sw
                JOIN gateway_workspaces w ON w.id = sw.workspace_id
                WHERE sw.session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return self._session_workspace_row_to_payload(row) if row else None

    def list_session_workspaces(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT sw.session_id, sw.cwd, sw.metadata_json,
                       sw.created_at AS binding_created_at,
                       sw.updated_at AS binding_updated_at,
                       sw.workspace_id AS id,
                       w.name, w.path, w.kind, w.created_at, w.updated_at
                FROM gateway_session_workspaces sw
                JOIN gateway_workspaces w ON w.id = sw.workspace_id
                ORDER BY sw.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._session_workspace_row_to_payload(row) for row in rows]

    def delete_session_workspaces(self, session_ids: list[str]) -> list[dict[str, Any]]:
        normalized = [str(session_id or "").strip() for session_id in session_ids]
        normalized = [session_id for session_id in dict.fromkeys(normalized) if session_id]
        if not normalized:
            return []
        removed: list[dict[str, Any]] = []
        with self._lock, self._connect() as conn:
            for session_id in normalized:
                row = conn.execute(
                    """
                    SELECT sw.session_id, sw.cwd, sw.metadata_json,
                           sw.created_at AS binding_created_at,
                           sw.updated_at AS binding_updated_at,
                           sw.workspace_id AS id,
                           w.name, w.path, w.kind, w.created_at, w.updated_at
                    FROM gateway_session_workspaces sw
                    JOIN gateway_workspaces w ON w.id = sw.workspace_id
                    WHERE sw.session_id = ?
                    """,
                    (session_id,),
                ).fetchone()
                if row:
                    removed.append(self._session_workspace_row_to_payload(row))
                conn.execute(
                    "DELETE FROM gateway_session_workspaces WHERE session_id = ?",
                    (session_id,),
                )
        return removed

    @staticmethod
    def _decode_json_object(raw: str | None) -> dict[str, Any]:
        try:
            value = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def _session_workspace_row_to_payload(self, row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        payload = dict(row)
        payload["metadata"] = self._decode_json_object(payload.pop("metadata_json", None))
        return payload

    @staticmethod
    def _encode_toolsets(toolsets: list[str] | None) -> str | None:
        if toolsets is None:
            return None
        return json.dumps(toolsets, ensure_ascii=False)

    @staticmethod
    def _decode_toolsets(raw: str | None) -> list[str] | None:
        if raw is None:
            return None
        try:
            values = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(values, list):
            return None
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            name = str(value or "").strip()
            if name and name not in seen:
                seen.add(name)
                result.append(name)
        return result

    def upsert_session_toolsets(
        self,
        *,
        session_id: str,
        enabled_toolsets: list[str] | None,
        disabled_toolsets: list[str] | None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO gateway_session_toolsets
                    (session_id, enabled_toolsets_json, disabled_toolsets_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    enabled_toolsets_json = excluded.enabled_toolsets_json,
                    disabled_toolsets_json = excluded.disabled_toolsets_json,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    self._encode_toolsets(enabled_toolsets),
                    self._encode_toolsets(disabled_toolsets),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM gateway_session_toolsets WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return self._session_toolsets_row_to_payload(row)

    def get_session_toolsets(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM gateway_session_toolsets WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return self._session_toolsets_row_to_payload(row) if row else None

    def _session_toolsets_row_to_payload(self, row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        payload = dict(row)
        payload["enabled_toolsets"] = self._decode_toolsets(
            payload.pop("enabled_toolsets_json", None)
        )
        payload["disabled_toolsets"] = self._decode_toolsets(
            payload.pop("disabled_toolsets_json", None)
        )
        return payload

    def list_workspaces(self, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM gateway_workspaces
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_artifact(
        self,
        *,
        artifact: dict[str, Any],
        session_id: str,
    ) -> dict[str, Any]:
        now = time.time()
        origin_json = json.dumps(artifact.get("origin") or {}, ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO gateway_artifacts
                    (id, workspace_id, path, relative_path, title, mime_type,
                     size_bytes, origin_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id, path) DO UPDATE SET
                    title = excluded.title,
                    mime_type = excluded.mime_type,
                    size_bytes = excluded.size_bytes,
                    origin_json = excluded.origin_json,
                    updated_at = excluded.updated_at
                """,
                (
                    artifact["id"],
                    artifact["workspace_id"],
                    artifact["path"],
                    artifact["relative_path"],
                    artifact["title"],
                    artifact["mime_type"],
                    int(artifact.get("size_bytes") or 0),
                    origin_json,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT a.*, w.name AS workspace_name, w.path AS workspace_path,
                       w.kind AS workspace_kind
                FROM gateway_artifacts a
                JOIN gateway_workspaces w ON w.id = a.workspace_id
                WHERE a.workspace_id = ? AND a.path = ?
                """,
                (artifact["workspace_id"], artifact["path"]),
            ).fetchone()
            artifact_id = row["id"] if row else artifact["id"]
            conn.execute(
                """
                INSERT INTO gateway_session_artifacts
                    (session_id, artifact_id, first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id, artifact_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at
                """,
                (session_id, artifact_id, now, now),
            )
        return self._artifact_row_to_payload(row)

    def list_artifacts(
        self,
        *,
        session_id: str | None = None,
        workspace_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if session_id:
            query = """
                SELECT a.*, w.name AS workspace_name, w.path AS workspace_path,
                       w.kind AS workspace_kind
                FROM gateway_session_artifacts sa
                JOIN gateway_artifacts a ON a.id = sa.artifact_id
                JOIN gateway_workspaces w ON w.id = a.workspace_id
                WHERE sa.session_id = ?
                ORDER BY sa.last_seen_at DESC
                LIMIT ?
            """
            params: tuple[Any, ...] = (session_id, limit)
        elif workspace_id:
            query = """
                SELECT a.*, w.name AS workspace_name, w.path AS workspace_path,
                       w.kind AS workspace_kind
                FROM gateway_artifacts a
                JOIN gateway_workspaces w ON w.id = a.workspace_id
                WHERE a.workspace_id = ?
                ORDER BY a.updated_at DESC
                LIMIT ?
            """
            params = (workspace_id, limit)
        else:
            query = """
                SELECT a.*, w.name AS workspace_name, w.path AS workspace_path,
                       w.kind AS workspace_kind
                FROM gateway_artifacts a
                JOIN gateway_workspaces w ON w.id = a.workspace_id
                ORDER BY a.updated_at DESC
                LIMIT ?
            """
            params = (limit,)

        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._artifact_row_to_payload(row) for row in rows]

    def delete_session_artifacts(self, session_ids: list[str]) -> dict[str, Any]:
        normalized = [str(session_id or "").strip() for session_id in session_ids]
        normalized = [session_id for session_id in dict.fromkeys(normalized) if session_id]
        if not normalized:
            return {
                "deleted_artifact_links": 0,
                "deleted_artifacts": 0,
                "deleted_artifact_ids": [],
                "physical_files_deleted": 0,
            }

        with self._lock, self._connect() as conn:
            placeholders = ",".join("?" for _ in normalized)
            linked_rows = conn.execute(
                f"""
                SELECT DISTINCT artifact_id
                FROM gateway_session_artifacts
                WHERE session_id IN ({placeholders})
                """,
                tuple(normalized),
            ).fetchall()
            candidate_ids = [str(row["artifact_id"] or "") for row in linked_rows]
            candidate_ids = [artifact_id for artifact_id in dict.fromkeys(candidate_ids) if artifact_id]
            deleted_links = int(
                conn.execute(
                    f"DELETE FROM gateway_session_artifacts WHERE session_id IN ({placeholders})",
                    tuple(normalized),
                ).rowcount
                or 0
            )
            deleted_artifact_ids: list[str] = []
            if candidate_ids:
                candidate_placeholders = ",".join("?" for _ in candidate_ids)
                orphan_rows = conn.execute(
                    f"""
                    SELECT a.id
                    FROM gateway_artifacts a
                    LEFT JOIN gateway_session_artifacts sa ON sa.artifact_id = a.id
                    WHERE a.id IN ({candidate_placeholders})
                      AND sa.artifact_id IS NULL
                    """,
                    tuple(candidate_ids),
                ).fetchall()
                for row in orphan_rows:
                    artifact_id = str(row["id"] or "")
                    if not artifact_id:
                        continue
                    deleted = int(
                        conn.execute(
                            "DELETE FROM gateway_artifacts WHERE id = ?",
                            (artifact_id,),
                        ).rowcount
                        or 0
                    )
                    if deleted:
                        deleted_artifact_ids.append(artifact_id)
        return {
            "deleted_artifact_links": deleted_links,
            "deleted_artifacts": len(deleted_artifact_ids),
            "deleted_artifact_ids": deleted_artifact_ids,
            "physical_files_deleted": 0,
        }

    def prune_artifacts(
        self,
        *,
        session_id: str = "",
        workspace_id: str = "",
        retention_days: int = DEFAULT_ARTIFACT_RETENTION_DAYS,
        max_artifacts_per_session: int = DEFAULT_ARTIFACT_MAX_PER_SESSION,
        max_artifacts_per_workspace: int = DEFAULT_ARTIFACT_MAX_PER_WORKSPACE,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Prune gateway artifact metadata without deleting workspace files.

        ``gateway_artifacts`` is an index/cache over files that live in the
        bound workspace. Deleting rows here must never imply deleting the user's
        actual files; product-level file deletion belongs to explicit artifact
        or workspace cleanup flows.
        """
        stable_filter = str(session_id or "").strip()
        workspace_filter = str(workspace_id or "").strip()
        cutoff = float(now or time.time()) - max(1, int(retention_days or 1)) * 86400
        per_session_cap = max(1, int(max_artifacts_per_session or DEFAULT_ARTIFACT_MAX_PER_SESSION))
        per_workspace_cap = max(1, int(max_artifacts_per_workspace or DEFAULT_ARTIFACT_MAX_PER_WORKSPACE))

        orphan_candidates: set[str] = set()

        def _delete_links(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> int:
            deleted = 0
            for row in rows:
                cursor = conn.execute(
                    """
                    DELETE FROM gateway_session_artifacts
                    WHERE session_id = ? AND artifact_id = ?
                    """,
                    (row["session_id"], row["artifact_id"]),
                )
                rowcount = int(cursor.rowcount or 0)
                deleted += rowcount
                if rowcount:
                    orphan_candidates.add(str(row["artifact_id"] or ""))
            return deleted

        def _delete_orphan_artifacts(
            conn: sqlite3.Connection,
            *,
            candidate_ids: set[str] | None = None,
        ) -> int:
            params: list[Any] = []
            workspace_clause = ""
            if workspace_filter:
                workspace_clause = "AND a.workspace_id = ?"
                params.append(workspace_filter)
            candidate_clause = ""
            if candidate_ids is not None:
                candidates = [artifact_id for artifact_id in sorted(candidate_ids) if artifact_id]
                if not candidates:
                    return 0
                candidate_clause = f"AND a.id IN ({','.join('?' for _ in candidates)})"
                params.extend(candidates)
            rows = conn.execute(
                f"""
                SELECT a.id
                FROM gateway_artifacts a
                LEFT JOIN gateway_session_artifacts sa ON sa.artifact_id = a.id
                WHERE sa.artifact_id IS NULL
                  {workspace_clause}
                  {candidate_clause}
                """,
                tuple(params),
            ).fetchall()
            deleted = 0
            for row in rows:
                cursor = conn.execute("DELETE FROM gateway_artifacts WHERE id = ?", (row["id"],))
                deleted += int(cursor.rowcount or 0)
            return deleted

        with self._lock, self._connect() as conn:
            link_params: list[Any] = [cutoff]
            link_clauses = ["sa.last_seen_at < ?"]
            if stable_filter:
                link_clauses.append("sa.session_id = ?")
                link_params.append(stable_filter)
            if workspace_filter:
                link_clauses.append("a.workspace_id = ?")
                link_params.append(workspace_filter)
            aged_links = conn.execute(
                f"""
                SELECT sa.session_id, sa.artifact_id
                FROM gateway_session_artifacts sa
                JOIN gateway_artifacts a ON a.id = sa.artifact_id
                WHERE {' AND '.join(link_clauses)}
                ORDER BY sa.last_seen_at ASC
                """,
                tuple(link_params),
            ).fetchall()
            deleted_links = _delete_links(conn, aged_links)

            sessions_sql = "SELECT DISTINCT session_id FROM gateway_session_artifacts"
            sessions_params: tuple[Any, ...] = ()
            if stable_filter:
                sessions_sql += " WHERE session_id = ?"
                sessions_params = (stable_filter,)
            sessions = [
                str(row["session_id"] or "")
                for row in conn.execute(sessions_sql, sessions_params).fetchall()
            ]
            for sid in sessions:
                params: list[Any] = [sid]
                workspace_clause = ""
                if workspace_filter:
                    workspace_clause = "AND a.workspace_id = ?"
                    params.append(workspace_filter)
                rows = conn.execute(
                    f"""
                    SELECT sa.session_id, sa.artifact_id
                    FROM gateway_session_artifacts sa
                    JOIN gateway_artifacts a ON a.id = sa.artifact_id
                    WHERE sa.session_id = ?
                      {workspace_clause}
                    ORDER BY sa.last_seen_at DESC, sa.artifact_id DESC
                    """,
                    tuple(params),
                ).fetchall()
                deleted_links += _delete_links(conn, rows[per_session_cap:])

            workspaces_sql = "SELECT DISTINCT workspace_id FROM gateway_artifacts"
            workspace_params: tuple[Any, ...] = ()
            if workspace_filter:
                workspaces_sql += " WHERE workspace_id = ?"
                workspace_params = (workspace_filter,)
            workspaces = [
                str(row["workspace_id"] or "")
                for row in conn.execute(workspaces_sql, workspace_params).fetchall()
            ]
            deleted_artifacts = _delete_orphan_artifacts(
                conn,
                candidate_ids=orphan_candidates if stable_filter else None,
            )
            for wid in workspaces:
                rows = conn.execute(
                    """
                    SELECT id
                    FROM gateway_artifacts
                    WHERE workspace_id = ?
                    ORDER BY updated_at DESC, id DESC
                    """,
                    (wid,),
                ).fetchall()
                overflow_ids = [str(row["id"] or "") for row in rows[per_workspace_cap:]]
                for artifact_id in overflow_ids:
                    deleted_links += int(
                        conn.execute(
                            "DELETE FROM gateway_session_artifacts WHERE artifact_id = ?",
                            (artifact_id,),
                        ).rowcount or 0
                    )
                    deleted_artifacts += int(
                        conn.execute(
                            "DELETE FROM gateway_artifacts WHERE id = ?",
                            (artifact_id,),
                        ).rowcount or 0
                    )
            deleted_artifacts += _delete_orphan_artifacts(
                conn,
                candidate_ids=orphan_candidates if stable_filter else None,
            )

        return {
            "deleted_artifact_links": deleted_links,
            "deleted_artifacts": deleted_artifacts,
            "retention_days": int(retention_days or DEFAULT_ARTIFACT_RETENTION_DAYS),
            "max_artifacts_per_session": per_session_cap,
            "max_artifacts_per_workspace": per_workspace_cap,
            "physical_files_deleted": 0,
        }

    @staticmethod
    def _artifact_row_to_payload(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        payload = dict(row)
        try:
            payload["origin"] = json.loads(payload.pop("origin_json") or "{}")
        except json.JSONDecodeError:
            payload["origin"] = {}
        workspace_name = payload.pop("workspace_name", "")
        workspace_path = payload.pop("workspace_path", "")
        workspace_kind = payload.pop("workspace_kind", "")
        payload["workspace"] = {
            "id": payload.get("workspace_id") or "",
            "name": workspace_name,
            "path": workspace_path,
            "kind": workspace_kind,
        }
        return payload


_DEFAULT_STORES: dict[str, GatewayStateStore] = {}
_READONLY_STORES: dict[str, GatewayStateStore] = {}
_DEFAULT_STORE_LOCK = threading.Lock()


def get_gateway_state_store(*, create_if_missing: bool = True) -> GatewayStateStore | None:
    home = Path(get_hermes_home()).expanduser()
    key = str(home.resolve())
    with _DEFAULT_STORE_LOCK:
        if create_if_missing:
            store = _DEFAULT_STORES.get(key)
            if store is None:
                store = GatewayStateStore(home / "tui-gateway" / "state.db")
                _DEFAULT_STORES[key] = store
            return store
        db_path = home / "tui-gateway" / "state.db"
        if not db_path.exists():
            return None
        store = _READONLY_STORES.get(key)
        if store is None:
            store = GatewayStateStore(db_path, create_if_missing=False)
            _READONLY_STORES[key] = store
        return store
